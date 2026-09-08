"""训练工作台的源码参数分析、范围推荐和结果索引。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


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


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _comment_for_line(source_lines: list[str], line: int) -> str:
    text = source_lines[line - 1].split("#", 1)
    return text[1].strip() if len(text) == 2 else ""


def extract_strategy_params(source_path: Path | str) -> dict[str, Any]:
    """从策略类的 params = {...} 中提取可静态解析的默认值。"""

    path = Path(source_path)
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    candidates: list[ast.Dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "params" for target in targets):
                value = node.value
                if isinstance(value, ast.Dict):
                    candidates.append(value)
    if not candidates:
        return {}
    result: dict[str, Any] = {}
    for key_node, value_node in zip(candidates[-1].keys, candidates[-1].values):
        key = _literal(key_node)
        value = _literal(value_node)
        if isinstance(key, str) and value is not None:
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
    params_node: ast.Dict | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "params" for target in node.targets
        ) and isinstance(node.value, ast.Dict):
            params_node = node.value
    if params_node is None:
        return []
    suggestions: list[ParameterSuggestion] = []
    for key_node, value_node in zip(params_node.keys, params_node.values):
        name = _literal(key_node)
        value = _literal(value_node)
        if not isinstance(name, str) or value is None:
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


_METRIC_RE = re.compile(r"Best Training Score \(([^)]+)\):\s*([^\s]+)")
_PARAMS_RE = re.compile(r"^\s*Params:\s*(\{.*\})\s*$")


def _read_log_results(path: Path) -> list[TrainingResult]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    results: list[TrainingResult] = []
    for index, line in enumerate(lines):
        match = _METRIC_RE.search(line)
        if not match:
            continue
        metric, score = match.groups()
        params: dict[str, Any] = {}
        # 训练摘要中间可能包含整段测试集报告，不能用过短的固定窗口；
        # 只在下一个指标开始前查找，避免把后一个结果的参数错配过来。
        for candidate in lines[index + 1:]:
            if _METRIC_RE.search(candidate):
                break
            params_match = _PARAMS_RE.match(candidate)
            if params_match:
                try:
                    parsed = _literal(ast.parse(params_match.group(1), mode="eval").body)
                except (SyntaxError, ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    params = parsed
                break
        result_id = f"{path.name}:{index + 1}:{metric}"
        modified = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        results.append(TrainingResult(result_id, str(path), metric, score, params, modified))
    return results


def scan_training_results(root: Path | str) -> list[TrainingResult]:
    """扫描优化日志并按最近修改时间返回可恢复结果。"""

    directory = Path(root)
    if directory.name != "optimizer":
        directory = directory / ".data" / "optimizer"
    if not directory.is_dir():
        return []
    results = [item for path in directory.glob("optimizer_terminal_*.log") for item in _read_log_results(path)]
    return sorted(results, key=lambda item: item.modified_at, reverse=True)


class TrainingSelectionStore:
    """在 .data 中保存用户选中的训练结果版本。"""

    def __init__(self, project_root: Path | str) -> None:
        self.path = Path(project_root) / ".data" / "command_center" / "training_selections.json"

    def load(self) -> set[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return set(value.get("selected", [])) if isinstance(value, dict) else set()
        except (OSError, ValueError, TypeError):
            return set()

    def save(self, selected: Iterable[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"selected": sorted(set(selected)), "updated_at": datetime.now().isoformat(timespec="seconds")}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def result_to_params(result: TrainingResult) -> dict[str, Any]:
    """返回训练结果中适合注入 --params 的参数。"""

    return dict(result.params)
