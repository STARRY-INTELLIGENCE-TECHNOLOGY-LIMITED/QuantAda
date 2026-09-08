"""训练工作台的源码参数分析、范围推荐和结果索引。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


_UNPARSED = object()


@dataclass(frozen=True)
class ParameterSuggestion:
    """单个策略参数及推荐的 Optuna 搜索定义。"""

    name: str
    current: Any
    value_type: str
    recommendation: dict[str, Any]
    source_line: int
    comment: str = ""


@dataclass(frozen=True)
class TrainingResult:
    """从优化器终端日志恢复的一条训练结果。"""

    result_id: str
    path: str
    metric: str
    score: str
    params: dict[str, Any]
    modified_at: str
    selected: bool = False
    main_eval: dict[str, str] | None = None
    test_set: dict[str, str] | None = None


def _literal(node: ast.AST | None) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return _UNPARSED


def _comment_for_line(source_lines: list[str], line: int) -> str:
    text = source_lines[line - 1].split("#", 1)
    return text[1].strip() if len(text) == 2 else ""


def _param_items(node: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """返回字典字面量或 dict(...) 调用中的静态键值。"""

    if isinstance(node, ast.Dict):
        return list(zip(node.keys, node.values))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return [
            (ast.copy_location(ast.Constant(keyword.arg), keyword), keyword.value)
            for keyword in node.keywords
            if keyword.arg is not None
        ]
    return []


def extract_strategy_params(source_path: Path | str) -> dict[str, Any]:
    """从策略类的 params = {...} 中提取可静态解析的默认值。"""

    path = Path(source_path)
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    candidates: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "params" for target in targets):
                value = node.value
                if _param_items(value):
                    candidates.append(value)
    if not candidates:
        return {}
    result: dict[str, Any] = {}
    for key_node, value_node in _param_items(candidates[-1]):
        key = _literal(key_node)
        value = _literal(value_node)
        if isinstance(key, str) and value is not _UNPARSED:
            result[key] = value
    return result


def _suggestion(name: str, value: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(value, bool):
        return "bool", {"type": "categorical", "choices": [False, True]}
    if isinstance(value, int) and not isinstance(value, bool):
        spread = max(2, int(abs(value) * 0.5))
        low = max(1, value - spread) if value > 0 else value - spread
        high = max(value + 1, value + spread)
        step = max(1, int(round(spread / 5)))
        return "int", {"type": "int", "low": low, "high": high, "step": step}
    if isinstance(value, float):
        spread = max(0.1, abs(value) * 0.5)
        step = 0.01 if spread < 0.2 else 0.1
        return "float", {
            "type": "float",
            "low": round(value - spread, 8),
            "high": round(value + spread, 8),
            "step": step,
        }
    if isinstance(value, str):
        return "str", {"type": "categorical", "choices": [value]}
    return type(value).__name__, {"type": "categorical", "choices": [value]}


def recommend_ranges(source_path: Path | str) -> list[ParameterSuggestion]:
    """根据默认值类型和量级生成保守、可编辑的搜索范围。"""

    path = Path(source_path)
    source_lines = path.read_text(encoding="utf-8-sig").splitlines()
    tree = ast.parse("\n".join(source_lines), filename=str(path))
    params_node: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "params" for target in targets):
                value = node.value
                if value is not None and _param_items(value):
                    params_node = value
    if params_node is None:
        return []
    suggestions: list[ParameterSuggestion] = []
    for key_node, value_node in _param_items(params_node):
        name = _literal(key_node)
        value = _literal(value_node)
        if not isinstance(name, str) or value is _UNPARSED:
            continue
        value_type, recommendation = _suggestion(name, value)
        suggestions.append(
            ParameterSuggestion(
                name=name,
                current=value,
                value_type=value_type,
                recommendation=recommendation,
                source_line=getattr(key_node, "lineno", 0),
                comment=_comment_for_line(source_lines, getattr(key_node, "lineno", 0)),
            )
        )
    return suggestions


def suggestions_to_dict(suggestions: Iterable[ParameterSuggestion]) -> dict[str, dict[str, Any]]:
    """将建议列表转换为 --opt_params 所需的字典。"""

    return {item.name: dict(item.recommendation) for item in suggestions}


def recommendation_notes(suggestions: Iterable[ParameterSuggestion]) -> list[str]:
    """生成需要人工确认的范围关系提示，不自动修改用户范围。"""

    items = list(suggestions)
    names = {item.name for item in items}
    notes: list[str] = []
    for name in sorted(names):
        if name.endswith("_a") and name[:-2] + "_b" in names:
            notes.append(f"请确认 {name} < {name[:-2]}_b 的周期关系")
    for item in items:
        if item.value_type == "str" and item.recommendation.get("type") == "categorical":
            choices = item.recommendation.get("choices", [])
            if len(choices) <= 1:
                notes.append(f"{item.name} 当前只有一个固定类别，不会增加搜索维度")
    return notes


_METRIC_RE = re.compile(r"Best Training Score \(([^)]+)\):\s*([^\s]+)")
_PARAMS_RE = re.compile(r"^\s*Params:\s*(\{.*)")
_SUMMARY_RE = re.compile(r"^\s*SUMMARY OF BEST CONFIGURATION\s*$")
_SUMMARY_FIELD_RE = re.compile(
    r"^\s*(MainEval|TestSet|Annual|Drawdown|Calmar|Sharpe|Trades|WinRate|PF):\s*(.*)$"
)


def _parse_params_block(lines: list[str], start: int, limit: int) -> dict[str, Any]:
    """解析 Params 行及其可能的终端换行续行。"""

    first = _PARAMS_RE.match(lines[start])
    if not first:
        return {}
    chunks = [first.group(1).strip()]
    balance = chunks[0].count("{") - chunks[0].count("}")
    index = start + 1
    while balance > 0 and index < limit:
        chunk = lines[index].strip()
        if chunk:
            chunks.append(chunk)
            balance += chunk.count("{") - chunk.count("}")
        index += 1
    try:
        parsed = ast.literal_eval(" ".join(chunks))
    except (SyntaxError, ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_summary_metrics(lines: list[str], start: int, limit: int) -> tuple[dict[str, str], dict[str, str]]:
    """从最佳配置摘要中提取 MainEval 和 TestSet 指标。"""

    main_eval: dict[str, str] = {}
    test_set: dict[str, str] = {}
    section: dict[str, str] | None = None
    for index in range(start, limit):
        if _SUMMARY_RE.match(lines[index]):
            continue
        match = _SUMMARY_FIELD_RE.match(lines[index])
        if not match:
            continue
        field, value = match.groups()
        if field == "MainEval":
            section = main_eval
            section["window"] = value.strip()
        elif field == "TestSet":
            section = test_set
            section["window"] = value.strip()
        elif section is not None:
            section[field.lower()] = value.strip()
    return main_eval, test_set


def _read_log_results(path: Path) -> list[TrainingResult]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    results: list[TrainingResult] = []
    score_indexes = [index for index, line in enumerate(lines) if _METRIC_RE.search(line)]
    for score_position, index in enumerate(score_indexes):
        line = lines[index]
        match = _METRIC_RE.search(line)
        if not match:
            continue
        metric, score = match.groups()
        params: dict[str, Any] = {}
        main_eval: dict[str, str] = {}
        test_set: dict[str, str] = {}
        # 训练摘要中间可能包含整段测试集报告，不能用过短的固定窗口；
        # 只在下一个指标开始前查找，避免把后一个结果的参数错配过来。
        next_score = score_indexes[score_position + 1] if score_position + 1 < len(score_indexes) else len(lines)
        for params_index in range(index + 1, next_score):
            if _PARAMS_RE.match(lines[params_index]):
                params = _parse_params_block(lines, params_index, next_score)
                if params:
                    break
        if not params:
            for params_index in range(max(0, index - 40), index):
                if _PARAMS_RE.match(lines[params_index]):
                    params = _parse_params_block(lines, params_index, index)
                    if params:
                        break
        for summary_index in range(index + 1, next_score):
            if _SUMMARY_RE.match(lines[summary_index]):
                main_eval, test_set = _parse_summary_metrics(lines, summary_index, next_score)
                break
        result_id = f"{path.name}:{index + 1}:{metric}"
        modified = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        results.append(
            TrainingResult(
                result_id,
                str(path),
                metric,
                score,
                params,
                modified,
                False,
                main_eval,
                test_set,
            )
        )
    return results


def scan_training_results(root: Path | str) -> list[TrainingResult]:
    """扫描优化日志并按最近修改时间返回可恢复结果。"""

    directory = Path(root)
    if directory.name != "optimizer":
        directory = directory / ".data" / "optimizer"
    if not directory.is_dir():
        return []
    results = [item for path in directory.glob("optimizer_terminal_*.log") for item in _read_log_results(path)]
    # Python 排序稳定：同一日志内保留训练指标出现顺序，跨日志按修改时间倒序。
    return sorted(results, key=lambda item: item.modified_at, reverse=True)


class TrainingSelectionStore:
    """在 .data 中保存用户选中的训练结果版本。"""

    def __init__(self, project_root: Path | str) -> None:
        self.path = Path(project_root) / ".data" / "command_center" / "training_selections.json"

    def load(self) -> set[str]:
        payload = self._read()
        return set(payload.get("selected", [])) if isinstance(payload, dict) else set()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def load_results(self) -> dict[str, dict[str, Any]]:
        """读取已保存的训练结果快照。"""

        value = self._read().get("results", {})
        return value if isinstance(value, dict) else {}

    def save(self, selected: Iterable[str]) -> None:
        payload = self._read()
        payload["selected"] = sorted(set(selected))
        payload.setdefault("results", {})
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_result(self, result: TrainingResult) -> None:
        """保存一个训练结果快照并将其标记为选中版本。"""

        payload = self._read()
        selected = set(payload.get("selected", []))
        selected.add(result.result_id)
        results = payload.get("results", {})
        if not isinstance(results, dict):
            results = {}
        results[result.result_id] = {
            "result_id": result.result_id,
            "path": result.path,
            "metric": result.metric,
            "score": result.score,
            "params": result.params,
            "modified_at": result.modified_at,
            "main_eval": result.main_eval or {},
            "test_set": result.test_set or {},
        }
        payload.update(
            {
                "selected": sorted(selected),
                "results": results,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def result_to_params(result: TrainingResult) -> dict[str, Any]:
    """返回训练结果中适合注入 --params 的参数。"""

    return dict(result.params)
