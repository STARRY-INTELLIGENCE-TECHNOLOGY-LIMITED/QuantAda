"""生产服务器运行 Python 3.9：静态检查仓库代码不使用 3.10+ 才能导入或运行的写法。

CI 与开发机通常是更高版本，3.9 不兼容只会在服务器导入时暴露（例如 `time | None` 注解导致 run.py 启动即崩溃），
因此用 AST 扫描代替 3.9 解释器做回归门禁。规则见 AGENTS.md「Python 版本兼容」。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".data", "node_modules", "gmcache", "build", "dist"}
SELF = Path(__file__).resolve()

# 3.10+ 标准库/语法 API；按「名称 + 典型调用形态」匹配，避免误伤普通变量名。
BANNED_API = {
    "zip(strict=)": r"\bzip\([^)\n]*\bstrict\s*=",
    "dataclass(slots/kw_only)": r"@dataclass\([^)\n]*\b(slots|kw_only)\s*=",
    "itertools.pairwise": r"\bitertools\.pairwise\b|from itertools import[^\n]*\bpairwise\b",
    "typing 3.10+ 名称": (
        r"from typing import[^\n]*\b(ParamSpec|TypeAlias|Concatenate|TypeGuard|Self|Never|"
        r"LiteralString|assert_never|reveal_type|Required|NotRequired|Unpack|TypeVarTuple)\b"
    ),
    "datetime.UTC": r"\bdatetime\.UTC\b|from datetime import[^\n]*\bUTC\b",
    "tomllib": r"^\s*(import|from)\s+tomllib\b",
    "ExceptionGroup / except*": r"\bExceptionGroup\b|^\s*except\s*\*",
    "asyncio.TaskGroup / asyncio.timeout": r"\basyncio\.(TaskGroup|timeout)\b",
    "int.bit_count": r"\.bit_count\(\)",
}


def _python_files():
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT).parts
        if any(
            part in SKIP_DIRS or part.startswith((".venv", "venv", "pytest-cache", ".pytest"))
            for part in parts[:-1]
        ):
            continue
        if path.resolve() == SELF:
            continue
        yield path


def _has_future_annotations(tree):
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _uses_union_operator(node):
    return any(isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr) for item in ast.walk(node))


def _violations(path):
    rel = path.relative_to(ROOT).as_posix()
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # 无法解析的文件由其它测试/导入暴露，不在此重复报告
    found = []
    try:
        ast.parse(source, feature_version=(3, 9))
    except SyntaxError as exc:
        found.append(f"{rel}:{exc.lineno} 使用了 3.10+ 语法（如 match 或带括号的多 with）: {exc.msg}")

    annotation_ids = set()
    future = _has_future_annotations(tree)
    for annotation in _annotations(tree):
        annotation_ids.update(id(item) for item in ast.walk(annotation))
        if not future and _uses_union_operator(annotation):
            found.append(
                f"{rel}:{annotation.lineno} 注解使用 `X | Y` 但文件缺少 `from __future__ import annotations`"
            )
    for node in ast.walk(tree):
        if id(node) in annotation_ids:
            continue
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) in {"isinstance", "issubclass"}
            and len(node.args) == 2
            and _uses_union_operator(node.args[1])
            and any(isinstance(item, (ast.Name, ast.Attribute)) for item in ast.walk(node.args[1]))
            and not isinstance(node.args[1], ast.Tuple)
        ):
            found.append(f"{rel}:{node.lineno} isinstance/issubclass 使用 `A | B`，3.9 需改为元组")

    for label, pattern in BANNED_API.items():
        for match in re.finditer(pattern, source, flags=re.MULTILINE):
            line = source.count("\n", 0, match.start()) + 1
            found.append(f"{rel}:{line} 使用 3.10+ API: {label}")
    return found


def test_repository_code_stays_importable_on_python39():
    violations = [item for path in _python_files() for item in _violations(path)]
    assert not violations, "以下写法在生产 Python 3.9 上会失败:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def f(x: int | None): pass\n", "缺少 `from __future__ import annotations`"),
        ("from __future__ import annotations\ndef f(x: int | None): pass\n", None),
        ("isinstance(1, int | str)\n", "isinstance/issubclass"),
        ("for a, b in zip(x, y, strict=True): pass\n", "zip(strict=)"),
        ("match x:\n    case 1:\n        pass\n", "3.10+ 语法"),
        ("from datetime import UTC\n", "datetime.UTC"),
    ],
)
def test_scanner_detects_python310_only_constructs(tmp_path, monkeypatch, source, expected):
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "ROOT", tmp_path)
    found = _violations(sample)
    if expected is None:
        assert found == []
    else:
        assert any(expected in item for item in found), found
