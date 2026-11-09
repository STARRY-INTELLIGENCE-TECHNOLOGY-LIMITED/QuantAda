"""静态架构检查：样例风控/选股器/策略里的 ``self._xxx(...)`` 调用必须能解析到真实成员。

背景：``SampleTrendProtection`` 曾调用不存在的 ``self._check_trend_score``，
而风控链只记录异常，保护实际从未生效。这类缺陷在运行前即可通过 AST 发现。
"""

import ast
import importlib
import inspect
import pathlib
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_DIRS = ("risk_controls", "stock_selectors", "strategies")

# 动态属性白名单：(类名, 属性名) -> 原因。仅用于运行期才注入、静态无法解析的成员。
DYNAMIC_ALLOWLIST = {}


def _scan_modules():
    for scan_dir in SCAN_DIRS:
        for path in sorted((ROOT / scan_dir).rglob("*.py")):
            if path.name.startswith("__"):
                continue
            rel = path.relative_to(ROOT).with_suffix("")
            yield ".".join(rel.parts), path


def _instance_attrs(cls):
    """收集类及其 MRO 中所有 ``self.X = ...`` / 类级赋值定义的名字。"""
    names = set(dir(cls))
    for klass in cls.__mro__:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(klass)))
        except (OSError, TypeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self"):
                        names.add(sub.attr)
    return names


def _unresolved_self_calls(module_name, path):
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # 可选 SDK 缺失的模块无法静态解析，跳过而不是误报。
        pytest.skip(f"{module_name} not importable: {exc}")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        cls = getattr(module, node.name, None)
        if not inspect.isclass(cls):
            continue
        known = _instance_attrs(cls)
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            func = call.func
            if not (isinstance(func.value, ast.Name) and func.value.id == "self"):
                continue
            name = func.attr
            if not name.startswith("_") or (name.startswith("__") and name.endswith("__")):
                continue
            if name in known or (node.name, name) in DYNAMIC_ALLOWLIST:
                continue
            problems.append(f"{path.name}:{call.lineno} {node.name}.{name}")
    return problems


@pytest.mark.parametrize("module_name,path", list(_scan_modules()), ids=lambda v: str(getattr(v, "name", v)))
def test_self_private_calls_resolve(module_name, path):
    problems = _unresolved_self_calls(module_name, path)
    assert not problems, "self._xxx() 调用无法解析到类或其基类成员：\n" + "\n".join(problems)


def test_detects_missing_method(tmp_path, monkeypatch):
    """护栏自测：不存在的私有方法调用必须被发现，已定义的不误报。"""
    source = textwrap.dedent("""        class A:
            def __init__(self):
                self._attr = lambda: 1

            def _ok(self):
                return 1

            def run(self):
                self._ok()
                self._attr()
                return self._missing()
        """)
    (tmp_path / "fake_arch_mod.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    problems = _unresolved_self_calls("fake_arch_mod", tmp_path / "fake_arch_mod.py")
    assert problems == ["fake_arch_mod.py:11 A._missing"]
