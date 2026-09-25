"""QuantAda 本地命令工作台的轻量 HTTP API。

服务只依赖 Python 标准库，面向本机或用户明确暴露的地址提供命令生成、方案保存和
异步执行接口。真正执行仍复用 ``CommandExecutor``，不会通过 shell 拼接命令。
"""

from __future__ import annotations

import json
import argparse
import os
import threading
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import config
from optimizer.training_tasks import list_training_tasks, read_terminal_log_page, task_terminal_log, training_task_commands

from .catalog import CommandPreset, EnvRef, default_catalog
from .executor import CommandExecutor
from common.terminal_log import (
    OPTIMIZER_AI_ANALYSIS_END_MARKER,
    OPTIMIZER_AI_ANALYSIS_START_MARKER,
)
from .generator import (
    build_command,
    build_resume_command,
    command_response,
    detect_platform,
)
from .profiles import CommandProfileStore
from .web_static import get_index_html
from .training import (
    TrainingResult,
    TrainingSelectionStore,
    extract_strategy_params,
    recommend_ranges,
    recommendation_notes,
    scan_training_results,
    source_strategy_reference,
    suggestions_to_dict,
)


_MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
_MAX_RUN_OUTPUT_LINES = 100_000


def _jsonable(value: Any) -> Any:
    """把目录中的映射、元组等值转换成 JSON 可编码对象。"""

    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@dataclass
class RunRecord:
    """一次异步执行及其输出快照。"""

    run_id: str
    argv: list[str]
    output: list[str] = field(default_factory=list)
    running: bool = True
    return_code: int | None = None
    duration: float | None = None
    error: str | None = None


class CommandCenterService:
    """组合命令目录、方案存储和后台执行器，供 HTTP handler 调用。"""

    def __init__(self, project_root: Path | str | None = None, source_root: Path | str | None = None) -> None:
        self.project_root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
        configured_source_root = source_root or os.environ.get("QUANTADA_SOURCE_ROOT")
        self.source_root = Path(configured_source_root).expanduser().resolve() if configured_source_root else None
        # 公开目录只包含脱敏示例；本机 .data 私有目录作为覆盖层合并。
        self.catalog = default_catalog(self.project_root, self.source_root)
        self.profile_store = CommandProfileStore(self.project_root)
        self.training_store = TrainingSelectionStore(self.project_root)
        self.executor = CommandExecutor(self.project_root)
        self.platform = detect_platform()
        self.variables = self.catalog.variable_values()
        self.runs: dict[str, RunRecord] = {}
        self._active_run_id: str | None = None
        self._lock = threading.RLock()

    def _resolve_source_root(self, value: Any = None) -> Path | None:
        """解析可选外部策略仓库根目录。"""
        raw = value or self.source_root
        if not raw:
            return None
        root = Path(str(raw)).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"外部源码根目录不存在: {root}")
        return root

    @staticmethod
    def _module_exists_under_roots(reference: str, roots: list[Path]) -> bool:
        """按模块引用检查源码文件，避免导入执行私有策略。"""

        text = str(reference or "").strip()
        if not text:
            return True
        path = Path(text).expanduser()
        if path.suffix.lower() == ".py" and path.is_file():
            return True
        parts = [part for part in text.split(".") if part]
        if not parts:
            return False
        for root in roots:
            for end in range(len(parts), 0, -1):
                module_path = root.joinpath(*parts[:end])
                if module_path.with_suffix(".py").is_file():
                    return True
                if (module_path / "__init__.py").is_file():
                    return True
        return False

    def _module_resolves_from_pythonpath(
        self,
        reference: str,
        variables: Mapping[str, str],
    ) -> bool:
        """检查策略是否能从当前项目或 PYTHONPATH 源码根目录解析。"""

        roots = [self.project_root]
        raw_values = (
            str(variables.get("PYTHONPATH") or ""),
            str(os.environ.get("PYTHONPATH") or ""),
        )
        seen = {str(self.project_root).lower()}
        for raw in raw_values:
            for value in raw.split(os.pathsep):
                value = value.strip().strip('"')
                if not value:
                    continue
                try:
                    path = Path(value).expanduser().resolve()
                except OSError:
                    path = Path(value).expanduser()
                if not path.is_dir():
                    continue
                marker = str(path).lower()
                if marker not in seen:
                    seen.add(marker)
                    roots.append(path)
        return self._module_exists_under_roots(reference, roots)

    @staticmethod
    def _private_credential_names() -> tuple[str, str]:
        """返回允许保存到本机私有方案的 Futu 明文凭据字段。"""
        return "FUTU_TRADE_PASSWORD", "FUTU_TRADE_PASSWORD_MD5"

    def _private_credentials_from_payload(self, payload: Mapping[str, Any]) -> dict[str, str]:
        """合并当前请求和已保存方案的私有凭据。"""
        values: dict[str, str] = {}
        profile_id = str(
            payload.get("profile_id")
            or payload.get("preset_id")
            or payload.get("save_profile_id")
            or ""
        ).strip()
        if profile_id:
            values.update(self.profile_store.get_private_credentials(profile_id))
        incoming = payload.get("private_credentials")
        if isinstance(incoming, Mapping):
            for name in self._private_credential_names():
                if str(incoming.get(name) or ""):
                    values[name] = str(incoming[name])
        variables = payload.get("variables")
        if isinstance(variables, Mapping):
            for name in self._private_credential_names():
                if str(variables.get(name) or ""):
                    values[name] = str(variables[name])
        return values

    @staticmethod
    def _external_module_reference(value: str, root: Path | None) -> str:
        """把外部 .py 路径转换为可由 run.py 导入的模块名。"""
        if not root or not value.strip().lower().endswith('.py'):
            return value
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ValueError(f"策略源码不在外部源码根目录内: {path}") from None
        if relative.suffix.lower() != '.py':
            raise ValueError("策略源码必须是 Python 文件")
        parts = list(relative.with_suffix('').parts)
        if parts and parts[-1] == '__init__':
            parts.pop()
        return '.'.join(parts)

    def snapshot(self) -> dict[str, Any]:
        """返回前端初始化所需的完整目录快照。"""

        variables = {
            item.name: {
                "name": item.name,
                "value": self.variables.get(item.name, item.default),
                "default": item.default,
                "description": item.description,
            }
            for item in self.catalog.variables
        }
        profiles = [asdict(item) for item in self.profile_store.list()]
        presets = [
            {
                **asdict(item),
                "params": _jsonable(item.params),
                "config": _jsonable(item.config),
                "options": _jsonable(item.options),
            }
            for item in self.catalog.presets
        ]
        first = self.catalog.presets[0] if self.catalog.presets else None
        initial_form = {}
        if first is not None:
            initial_form = {
                "preset_id": first.preset_id,
                "strategy": first.strategy,
                "selection": first.selection or "",
                "data_source": first.data_source or "",
                "connect": first.connect or "",
                "market": first.market,
                "mode": first.mode,
                "config_profile": first.config_profile or "none",
                "params": _jsonable(dict(first.params)),
                "options": _jsonable(dict(first.options)),
                "config_override": _jsonable(self._resolve_env_refs(dict(first.config))),
                "variables": dict(self.variables),
            }
        return {
            "project_root": str(self.project_root),
            "source_root": str(self.source_root) if self.source_root else "",
            "platform": dict(self.platform),
            "variables": variables,
            "profiles": _jsonable(profiles),
            "presets": presets,
            "catalog": self.catalog_snapshot(),
            "form": initial_form,
        }

    @staticmethod
    def _variable_group(name: str) -> str:
        """按变量语义返回网页中的聚合分组。"""

        upper = name.upper()
        if upper.startswith("FUTU_"):
            return "Futu 连接"
        if upper.startswith("GM_"):
            return "GM 连接"
        if upper.startswith("IBKR_") or upper.startswith("IB_"):
            return "IBKR 连接"
        if ("WEBHOOK" in upper or "PUSH" in upper or "DINGTALK_" in upper
                or upper.startswith("ALARM_")):
            return "通知"
        if upper.endswith("TOKEN") or "SECRET" in upper or upper.endswith("KEY_PATH"):
            return "数据源凭据"
        if "LOT_SIZE" in upper or "ACCOUNT" in upper or "CASH" in upper:
            return "市场与数量"
        return "运行环境"

    def _gm_serv_parts(self, config_profile: str | None) -> tuple[str, str]:
        """从环境档案的 GM broker 连接中取出地址和端口，供方案反显。"""

        profile = self.catalog.profile(config_profile)
        if profile is None:
            return "", ""
        brokers = profile.config.get("BROKER_ENVIRONMENTS")
        if not isinstance(brokers, Mapping):
            return "", ""
        gm = brokers.get("gm_broker")
        if not isinstance(gm, Mapping):
            return "", ""
        for env in gm.values():
            if not isinstance(env, Mapping):
                continue
            serv = str(env.get("serv_addr") or "").strip()
            if not serv:
                continue
            host, separator, port = serv.rpartition(":")
            if separator:
                return host.strip(), port.strip()
            return serv, ""
        return "", ""

    def _annotate_gm_serv(self, item: dict[str, Any]) -> dict[str, Any]:
        """给方案列表补上 GM 地址/端口，加载方案时回填三个输入框。"""

        host, port = self._gm_serv_parts(str(item.get("config_profile") or ""))
        if host:
            item["gm_host"] = host
        if port:
            item["gm_port"] = port
        return item

    def catalog_snapshot(self) -> dict[str, Any]:
        """返回前端下拉框和分组变量使用的目录数据。"""

        variables = []
        for item in self.catalog.variables:
            variables.append(
                {
                    "name": item.name,
                    "default": item.default,
                    "value": self.variables.get(item.name, item.default),
                    "description": item.description,
                    "group": self._variable_group(item.name),
                }
            )
        presets = list(self.catalog.presets)
        known_data_sources = {
            "gm", "futu", "tiingo", "akshare", "tushare", "sxsc_tushare",
            "csv", "theta", "thetadata", "ibkr", "hybrid", "theta+futu",
        }
        profile_ids = {profile.profile_id for profile in self.catalog.profiles}
        return {
            "markets": sorted({item.market for item in presets} | {"自定义"}),
            "data_sources": sorted(
                known_data_sources
                | {
                    item.data_source
                    for item in presets
                    if item.data_source and (
                        item.data_source in known_data_sources or item.data_source not in profile_ids
                    )
                }
            ),
            "modes": [
                {"id": "backtest", "title": "回测"},
                {"id": "live", "title": "实盘"},
                {"id": "optimize", "title": "优化/训练"},
            ],
            "strategies": sorted({item.strategy for item in presets}),
            "selections": sorted({item.selection for item in presets if item.selection}),
            "presets": [
                {
                    "preset_id": item.preset_id,
                    "title": item.title,
                    "market": item.market,
                    "mode": item.mode,
                    "strategy": item.strategy,
                    "selection": item.selection or "",
                    "data_source": item.data_source or "",
                    "connect": item.connect or "",
                    "params": _jsonable(dict(item.params)),
                    "config_profile": item.config_profile or "none",
                    "config": _jsonable(dict(item.config)),
                    "options": _jsonable(dict(item.options)),
                    "description": item.description,
                }
                for item in presets
            ],
            "variables": variables,
            "config_profiles": [
                {
                    "id": item.profile_id,
                    "title": item.title,
                    "description": item.description,
                }
                for item in self.catalog.profiles
            ],
            "param_descriptions": {},
            "default_params": _jsonable(dict(presets[0].params)) if presets else {},
        }

    def profile_list(self) -> list[dict[str, Any]]:
        """返回内置命令和用户方案的统一列表。"""

        builtins: list[dict[str, Any]] = []
        for item in self.catalog.presets:
            config_override = self._resolve_env_refs(dict(item.config))
            profile = self.catalog.profile(item.config_profile)
            if profile is not None and "PRINT_PLAN" not in config_override and "PRINT_PLAN" in profile.config:
                config_override["PRINT_PLAN"] = self._resolve_env_refs(profile.config["PRINT_PLAN"])
            builtins.append(
                {
                    "profile_id": item.preset_id,
                    "name": item.title,
                    "builtin": item.origin == "README",
                    "readonly": True,
                    "strategy": item.strategy,
                    "selection": item.selection or "",
                    "data_source": item.data_source or "",
                    "connect": item.connect or "",
                    "market": item.market,
                    "mode": item.mode,
                    "params": _jsonable(dict(item.params)),
                    "options": _jsonable(dict(item.options)),
                    "config_override": _jsonable(config_override),
                    "config_profile": item.config_profile or "none",
                    "origin": item.origin,
                }
            )
        readme = [item for item in builtins if item.get("origin") == "README"]
        private = [item for item in builtins if item.get("origin") != "README"]
        stored = self.profile_store.list()
        stored_readme = [_jsonable(asdict(item)) for item in stored if item.origin == "README"]
        stored_user = [_jsonable(asdict(item)) for item in stored if item.origin not in {"README", "私有命令集"}]
        stored_private = [_jsonable(asdict(item)) for item in stored if item.origin == "私有命令集"]
        items = readme + stored_readme + stored_user + stored_private + private
        return [self._annotate_gm_serv(item) for item in items]

    def _resolve_env_refs(self, value: Any) -> Any:
        """将内置方案中的环境变量引用解析为当前会话值。"""

        if isinstance(value, EnvRef):
            resolved = self.variables.get(value.name, "")
            if value.value_type == "int":
                try:
                    return int(resolved)
                except (TypeError, ValueError):
                    return resolved
            if value.value_type in {"float", "number"}:
                try:
                    number = float(resolved)
                    return int(number) if value.value_type == "number" and number.is_integer() else number
                except (TypeError, ValueError):
                    return resolved
            return resolved
        if isinstance(value, Mapping):
            return {key: self._resolve_env_refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_env_refs(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._resolve_env_refs(item) for item in value)
        return value

    def training_tasks(self) -> list[dict[str, Any]]:
        """返回与 CLI 共用的训练任务列表；列表展示不暴露整份参数快照。"""
        tasks = list_training_tasks(self.project_root / config.DATA_PATH / "optuna")
        return [{key: value for key, value in task.items() if key != "recorded"} for task in tasks]

    def training_task_log(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """按页返回选中任务的最新终端日志，默认末尾；不接受客户端日志路径。"""
        task_id = str(payload.get("train_resume") or "")
        tasks = list_training_tasks(self.project_root / config.DATA_PATH / "optuna")
        task = next((item for item in tasks if item["task_id"] == task_id), None)
        if task is None:
            raise ValueError("Task not found. Refresh the list and select again.")
        path = task_terminal_log(task, tasks)
        if path is None:
            raise ValueError("No terminal log found for this task.")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.project_root.resolve())
        except ValueError:
            raise ValueError("Training log must stay inside the QuantAda project directory.") from None
        where = str(payload.get("where") or "").strip().lower()
        page = payload.get("page")
        if where in {"start", "middle", "end", "display"}:
            return read_terminal_log_page(resolved, where=where)
        if page not in (None, ""):
            return read_terminal_log_page(resolved, page=int(page))
        return read_terminal_log_page(resolved, where="end")

    def training_results(self) -> list[dict[str, Any]]:
        """合并日志和独立快照，日志删除后仍可恢复训练结果。"""

        selected = self.training_store.load()
        scanned = {item.result_id: item for item in scan_training_results(self.project_root)}
        for result_id, snapshot in self.training_store.load_results().items():
            if result_id in scanned or not isinstance(snapshot, Mapping):
                continue
            scanned[result_id] = TrainingResult(
                result_id=str(snapshot.get("result_id") or result_id),
                path=str(snapshot.get("path") or ""),
                metric=str(snapshot.get("metric") or ""),
                score=str(snapshot.get("score") or ""),
                params=dict(snapshot.get("params") or {}),
                modified_at=str(snapshot.get("modified_at") or ""),
                selected=True,
                main_eval=dict(snapshot.get("main_eval") or {}),
                test_set=dict(snapshot.get("test_set") or {}),
                metadata=dict(snapshot.get("metadata") or {}),
            )
        return [
            {**_jsonable(asdict(item)), "selected": item.result_id in selected}
            for item in sorted(scanned.values(), key=lambda item: item.modified_at, reverse=True)
        ]

    def training_log(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """返回选中训练结果对应的原始日志文本。"""

        result_id = str(payload.get("result_id") or "").strip()
        if not result_id:
            raise ValueError("result_id 不能为空")
        result = next((item for item in scan_training_results(self.project_root) if item.result_id == result_id), None)
        if result is None:
            snapshot = self.training_store.load_results().get(result_id)
            if isinstance(snapshot, Mapping):
                return {
                    "result_id": result_id,
                    "path": str(snapshot.get("path") or ""),
                    "content": "",
                    "truncated": False,
                    "marker_found": False,
                    "snapshot": _jsonable(snapshot),
                }
            raise ValueError("训练结果不存在或日志已被移除")
        path = Path(result.path).resolve()
        try:
            path.relative_to(self.project_root)
        except ValueError:
            raise ValueError("Training log must stay inside the QuantAda project directory.") from None
        if not path.is_file():
            raise ValueError("训练日志不存在")
        content = path.read_text(encoding="utf-8", errors="replace")
        marker_index = content.find(OPTIMIZER_AI_ANALYSIS_START_MARKER)
        if marker_index < 0:
            return {
                "result_id": result_id,
                "path": str(path),
                "content": "",
                "truncated": False,
                "marker_found": False,
            }
        content = content[marker_index + len(OPTIMIZER_AI_ANALYSIS_START_MARKER):]
        end_index = content.find(OPTIMIZER_AI_ANALYSIS_END_MARKER)
        if end_index >= 0:
            content = content[:end_index]
        content = content.lstrip("\r\n").rstrip()
        truncated = False
        max_chars = 4 * 1024 * 1024
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True
        return {
            "result_id": result_id,
            "path": str(path),
            "content": content,
            "truncated": truncated,
            "marker_found": True,
        }

    def analyze_training(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """静态分析策略源码并返回可编辑的范围建议。"""

        raw_path = str(payload.get("source_path") or payload.get("path") or "").strip()
        if not raw_path:
            raise ValueError("策略源码路径不能为空")
        source_root = self._resolve_source_root(payload.get('source_root'))
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = (source_root or self.project_root) / source_path
        source_path = source_path.resolve()
        try:
            source_path.relative_to(source_root or self.project_root)
        except ValueError:
            raise ValueError("策略源码必须位于配置的源码根目录内") from None
        if not source_path.is_file() or source_path.suffix.lower() != ".py":
            raise ValueError("策略源码文件不存在或不是 Python 文件")
        suggestions = recommend_ranges(source_path)
        ranges: list[dict[str, Any]] = []
        for item in suggestions:
            recommendation = dict(item.recommendation)
            ranges.append({"name": item.name, "current": item.current, "value_type": item.value_type, **recommendation})
        return {
            "source_path": str(source_path),
            "strategy": source_strategy_reference(source_path, source_root or self.project_root),
            "suggestions": _jsonable([asdict(item) for item in suggestions]),
            "ranges": _jsonable(ranges),
            "opt_params": _jsonable(suggestions_to_dict(suggestions)),
            "notes": recommendation_notes(suggestions),
        }

    def strategy_params(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """按策略模块路径静态读取类级 params，不导入或执行策略代码。"""

        raw_strategy = str(payload.get("strategy") or "").strip()
        if not raw_strategy:
            return {"strategy": "", "source_path": None, "params": {}}
        source_root = self._resolve_source_root(payload.get('source_root'))
        source_path = Path(raw_strategy)
        if source_path.suffix.lower() == ".py":
            if not source_path.is_absolute():
                source_path = (source_root or self.project_root) / source_path
        else:
            parts = [part for part in raw_strategy.split(".") if part]
            source_path = None
            search_root = source_root or self.project_root
            for end in range(len(parts), 0, -1):
                candidate = search_root.joinpath(*parts[:end]).with_suffix(".py")
                package_init = search_root.joinpath(*parts[:end], "__init__.py")
                if candidate.is_file():
                    source_path = candidate
                    break
                if package_init.is_file():
                    source_path = package_init
                    break
            if source_path is None:
                return {"strategy": raw_strategy, "source_path": None, "params": {}}
        source_path = source_path.resolve()
        try:
            source_path.relative_to(source_root or self.project_root)
        except ValueError:
            return {"strategy": raw_strategy, "source_path": None, "params": {}}
        if not source_path.is_file():
            return {"strategy": raw_strategy, "source_path": None, "params": {}}
        try:
            params = extract_strategy_params(source_path)
        except (OSError, SyntaxError, UnicodeError):
            params = {}
        return {"strategy": raw_strategy, "source_path": str(source_path), "params": _jsonable(params)}

    def select_training_result(self, payload: Mapping[str, Any], *, selected: bool = True) -> dict[str, Any]:
        """将扫描到的训练结果保存或取消为可应用版本。"""

        result_id = str(payload.get("result_id") or "").strip()
        if not result_id:
            raise ValueError("result_id 不能为空")
        results = {item.result_id: item for item in scan_training_results(self.project_root)}
        if result_id not in results:
            snapshot = self.training_store.load_results().get(result_id)
            if selected and isinstance(snapshot, Mapping):
                self.training_store.save(
                    [*self.training_store.load(), result_id]
                )
                return {"result_id": result_id, "selected": True}
            if not selected and isinstance(snapshot, Mapping):
                self.training_store.unselect(result_id)
                return {"result_id": result_id, "selected": False}
            raise ValueError("训练结果不存在或日志已被移除")
        if selected:
            metadata = payload.get("metadata")
            self.training_store.save_result(results[result_id], metadata if isinstance(metadata, Mapping) else None)
        else:
            self.training_store.unselect(result_id)
        return {"result_id": result_id, "selected": selected}

    def _preset_from_payload(self, payload: Mapping[str, Any]) -> CommandPreset:
        """从 preset_id 或表单字段构造命令预设。"""

        preset_id = str(payload.get("preset_id") or payload.get("profile_id") or "").strip()
        if preset_id:
            base_preset = None
            try:
                base_preset = self.catalog.preset(preset_id)
            except KeyError:
                saved = next(
                    (item for item in self.profile_store.list() if item.profile_id == preset_id),
                    None,
                )
                if saved is None:
                    raise ValueError(f"未知方案: {preset_id}") from None
                base_preset = CommandPreset(
                    preset_id=saved.profile_id,
                    title=saved.name,
                    market=saved.market,
                    mode=saved.mode,
                    strategy=saved.strategy,
                    selection=saved.selection or None,
                    data_source=saved.data_source or None,
                    params=saved.params,
                    config_profile=saved.config_profile or None,
                    config=saved.config_override,
                    connect=saved.connect or None,
                    options={**saved.options, "desc": saved.name},
                    description="已保存方案",
                )
            # 方案 ID 只作为当前表单的基线；工作台允许用户在加载后编辑
            # 模式、参数、数据源和选项，不能再次用保存快照覆盖这些显式值。
            def payload_mapping(name, fallback):
                value = payload.get(name)
                return dict(value) if isinstance(value, Mapping) else fallback

            return replace(
                base_preset,
                strategy=str(payload["strategy"]) if "strategy" in payload else base_preset.strategy,
                selection=(str(payload["selection"]) or None)
                if "selection" in payload else base_preset.selection,
                data_source=(str(payload["data_source"]) or None)
                if "data_source" in payload else base_preset.data_source,
                params=payload_mapping("params", base_preset.params),
                config_profile=str(payload["config_profile"])
                if "config_profile" in payload else base_preset.config_profile,
                config=payload_mapping(
                    "config",
                    payload_mapping("config_override", base_preset.config),
                ),
                connect=(str(payload["connect"]) or None)
                if "connect" in payload else base_preset.connect,
                mode=str(payload["mode"]) if "mode" in payload else base_preset.mode,
                market=str(payload["market"]) if "market" in payload else base_preset.market,
                options=payload_mapping("options", base_preset.options),
            )
        strategy = str(payload.get("strategy") or "").strip()
        if not strategy:
            raise ValueError("策略不能为空")
        def mapping(name: str) -> dict[str, Any]:
            value = payload.get(name, {})
            return dict(value) if isinstance(value, Mapping) else {}
        return CommandPreset(
            preset_id="web-custom",
            title=str(payload.get("desc") or payload.get("name") or "自定义命令"),
            market=str(payload.get("market") or "自定义"),
            mode=str(payload.get("mode") or "backtest"),
            strategy=strategy,
            selection=str(payload.get("selection") or "") or None,
            data_source=str(payload.get("data_source") or "") or None,
            params=mapping("params"),
            config_profile=str(payload.get("config_profile") or "none") or None,
            config=(
                dict(payload.get("config"))
                if isinstance(payload.get("config"), Mapping)
                else (
                    dict(payload.get("config_override"))
                    if isinstance(payload.get("config_override"), Mapping)
                    else {}
                )
            ),
            connect=str(payload.get("connect") or "") or None,
            options=mapping("options"),
            description=str(payload.get("description") or ""),
        )

    def generate(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """生成命令和可复制文本。"""

        if "train_resume" in payload:
            task_id = str(payload.get("train_resume") or "")
            tasks = list_training_tasks(self.project_root / config.DATA_PATH / "optuna")
            task = next((item for item in tasks if item["task_id"] == task_id), None)
            if task is None:
                raise ValueError("Task not found. Refresh the list and select again.")
            if not task["resumable"]:
                raise ValueError(task["reason"])
            variables = dict(self.variables)
            if isinstance(payload.get("variables"), Mapping):
                variables.update(payload["variables"])
            generated = build_resume_command(
                task, variables, self.project_root, self._resolve_source_root(payload.get("source_root")),
            )
            shell = str(payload.get("shell") or self.platform["shell"])
            response = command_response(
                generated, shell,
                str(payload.get("platform") or self.platform["system"]),
            )
            response.update(training_task_commands(task, shell))
            return _jsonable(response)

        preset = self._preset_from_payload(payload)
        source_value = payload.get('source_root')
        if not source_value:
            requested_id = str(payload.get('preset_id') or payload.get('profile_id') or '').strip()
            source_value = next(
                (item.source_root for item in self.profile_store.list() if item.profile_id == requested_id),
                None,
            )
        source_root = self._resolve_source_root(source_value)
        if source_root:
            preset = replace(
                preset,
                strategy=self._external_module_reference(preset.strategy, source_root),
                selection=self._external_module_reference(preset.selection or '', source_root) or None,
            )
        if preset.mode not in {"backtest", "live", "optimize"}:
            raise ValueError("运行模式必须是 backtest、live 或 optimize")
        # run.py 没有独立的 mode 参数：connect 和 opt_params 才是实际分流开关。
        # 在工作台提前拦截不一致组合，避免用户看到“实盘/优化”却实际执行回测。
        if preset.mode == "live" and not preset.connect:
            raise ValueError("实盘模式必须填写连接配置（--connect broker:env）")
        if preset.connect and preset.mode != "live":
            raise ValueError("填写连接配置后必须将运行模式设为实盘")
        if preset.mode == "optimize" and not preset.options.get("opt_params"):
            raise ValueError("优化模式必须先设置优化参数范围（--opt_params）")
        if preset.mode in {"backtest", "live"} and preset.options.get("opt_params"):
            raise ValueError("当前模式包含优化参数；请切换为优化/训练模式")
        variables = dict(self.variables)
        incoming = payload.get("variables")
        if isinstance(incoming, Mapping):
            variables.update({str(key): str(value) for key, value in incoming.items()})
        private_credentials = (
            {}
            if bool(payload.get("clear_private_credentials"))
            else self._private_credentials_from_payload(payload)
        )
        # 工作台按用户要求原样展示和复制 Token、Webhook 及 Futu 私有凭据。
        # 私有凭据仍独立保存于本机方案文件，但不再在预览阶段隐藏。
        variables.update(private_credentials)
        shell = str(payload.get("shell") or self.platform.get("shell") or "Bash")
        platform_name = str(payload.get("platform") or self.platform.get("system") or "")
        variables = dict(variables)
        if source_root:
            existing_pythonpath = str(variables.get('PYTHONPATH') or os.environ.get('PYTHONPATH') or '')
            variables['PYTHONPATH'] = os.pathsep.join(
                item for item in (str(source_root), existing_pythonpath) if item
            )
        generated = build_command(
            self.catalog,
            preset,
            variables,
            self.project_root,
            # 实盘连接缺少凭据时必须在工作台阻断，不能把空值交给运行时
            # 继续尝试连接；回测/优化仍允许先生成带警告的命令。
            strict=bool(payload.get("strict", False)) or preset.mode == "live",
        )
        warnings = list(generated.warnings)
        # 校验私有策略包的实际可解析性，避免把导入失败留到执行阶段。
        validation_root = source_root or self.project_root
        for reference, label in ((preset.strategy, '策略'), (preset.selection, '选股器')):
            if reference and '.' in str(reference):
                package = str(reference).split('.')[0]
                if not (validation_root / package).exists():
                    if self._module_resolves_from_pythonpath(str(reference), variables):
                        continue
                    if source_root:
                        message = f"{label}模块 {reference} 不在 source_root={source_root} 内"
                    else:
                        message = (
                            f"{label}模块 {reference} 不在当前项目或 PYTHONPATH 中；"
                            "请配置 source_root 或 PYTHONPATH 外部源码根目录"
                        )
                    if preset.origin == "私有命令集":
                        raise ValueError(message)
                    warnings.append(message)
        return _jsonable(command_response(replace(generated, warnings=tuple(warnings)), shell, platform_name))

    def save_profile(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """保存当前命令方案，环境变量只存在于当前请求，不写入方案。"""

        form = payload.get("form")
        if isinstance(form, Mapping):
            payload = {**dict(form), **{key: value for key, value in payload.items() if key != "form"}}
        name = str(payload.get("name") or payload.get("desc") or "").strip()
        if not name:
            raise ValueError("方案名称不能为空")
        preset = self._preset_from_payload(payload)
        options = dict(preset.options)
        options["desc"] = name
        config_override = payload.get("config_override", payload.get("config", preset.config))
        if not isinstance(config_override, Mapping):
            config_override = {}
        private_credentials = (
            {}
            if bool(payload.get("clear_private_credentials"))
            else self._private_credentials_from_payload(payload)
        )
        config_override = {
            str(key): value
            for key, value in config_override.items()
            if str(key) not in self._private_credential_names()
        }
        source_root_value = str(payload.get("source_root") or "")
        if not source_root_value:
            update_id = str(payload.get("save_profile_id") or "").strip()
            source_root_value = next(
                (item.source_root for item in self.profile_store.list() if item.profile_id == update_id),
                "",
            )
        profile = self.profile_store.make_profile(
            name,
            strategy=preset.strategy,
            selection=preset.selection or "",
            data_source=preset.data_source or "",
            connect=preset.connect or "",
            market=preset.market,
            mode=preset.mode,
            shell=str(payload.get("shell") or self.platform.get("shell") or ""),
            system=str(payload.get("platform") or self.platform.get("system") or ""),
            config_profile=preset.config_profile or "none",
            params=dict(preset.params),
            options=options,
            config_override=dict(config_override),
            origin="用户方案",
            source_root=source_root_value,
        )
        save_profile_id = str(payload.get("save_profile_id") or "").strip()
        if save_profile_id:
            existing = next(
                (item for item in self.profile_store.list() if item.profile_id == save_profile_id),
                None,
            )
            if existing is None:
                try:
                    self.catalog.preset(save_profile_id)
                except KeyError:
                    pass
                else:
                    raise ValueError("内置方案只读，请使用新的方案别名")
                raise ValueError("待更新方案不存在")
            if existing.origin != "用户方案":
                raise ValueError("内置方案只读，请使用新的方案别名")
            if existing.name != name:
                raise ValueError("修改方案别名时请使用另存为")
            profile = replace(profile, profile_id=save_profile_id)
        self.profile_store.save(profile)
        self.profile_store.set_private_credentials(profile.profile_id, private_credentials)
        return asdict(profile)

    def execute(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """异步执行生成的命令，并返回用于轮询的 run_id。"""

        generated = self.generate(payload)
        with self._lock:
            if self.executor.running:
                raise RuntimeError("已有命令正在运行")
            run_id = uuid.uuid4().hex
            record = RunRecord(run_id, list(generated["argv"]))
            self.runs[run_id] = record
            self._active_run_id = run_id
            completed_ids = [key for key, value in self.runs.items() if not value.running and key != run_id]
            for old_id in completed_ids[:-20]:
                self.runs.pop(old_id, None)

        def on_line(line: str) -> None:
            with self._lock:
                if len(record.output) < _MAX_RUN_OUTPUT_LINES:
                    record.output.append(str(line))
                elif len(record.output) == _MAX_RUN_OUTPUT_LINES:
                    record.output.append("[CommandCenter] 输出过长，后续内容已截断。")

        def on_done(return_code: int, duration: float) -> None:
            with self._lock:
                record.running = False
                record.return_code = int(return_code)
                record.duration = float(duration)
                if self._active_run_id == run_id:
                    self._active_run_id = None

        try:
            self.executor.start(generated["argv"], environ=generated["variables"], on_line=on_line, on_done=on_done)
        except Exception as exc:
            with self._lock:
                record.running = False
                record.error = str(exc)
                if self._active_run_id == run_id:
                    self._active_run_id = None
            raise
        return {
            "run_id": run_id,
            "running": True,
            "argv": generated["argv"],
            "command": generated["command"],
            "display_command": generated["display_command"],
        }

    def run_snapshot(self, run_id: str, offset: int = 0) -> dict[str, Any]:
        """返回运行输出；offset 用于前端增量轮询。"""

        with self._lock:
            record = self.runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            output = record.output[max(0, int(offset)):]
            return {
                "run_id": run_id,
                "output": output,
                "next_offset": len(record.output),
                "running": record.running,
                "return_code": record.return_code,
                "duration": record.duration,
                "error": record.error,
            }

    def stop(self, run_id: str) -> bool:
        """停止当前运行；仅允许停止当前执行器关联的进程。"""

        with self._lock:
            record = self.runs.get(run_id)
            if record is None or not record.running or self._active_run_id != run_id:
                return False
            return self.executor.stop()


class _RequestHandler(BaseHTTPRequestHandler):
    """将 HTTP 请求转发到 CommandCenterService。"""

    server: "CommandCenterHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def service(self) -> CommandCenterService:
        return self.server.service

    def _send(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = HTTPStatus.OK) -> None:
        """返回内置单页应用。"""

        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            raise ValueError("Content-Length 无效") from None
        if length < 0 or length > _MAX_REQUEST_BODY_BYTES:
            # 先消费有限请求体，避免 Windows HTTPServer 因未读完 body
            # 直接关闭连接时向客户端返回 ConnectionAbortedError。
            try:
                self.rfile.read(min(max(length, 0), _MAX_REQUEST_BODY_BYTES + 1))
            except Exception:
                pass
            raise ValueError("请求体超过 4 MiB 限制")
        if length <= 0:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        return dict(value) if isinstance(value, Mapping) else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in {"/", "/index.html"}:
                self._send_html(get_index_html())
            elif path == "/api/state":
                self._send(self.service.snapshot())
            elif path == "/api/catalog":
                self._send({"catalog": self.service.catalog_snapshot()})
            elif path == "/api/profiles":
                self._send({"profiles": self.service.profile_list()})
            elif path.startswith("/api/profiles/"):
                profile_id = path.rsplit("/", 1)[-1]
                profile = next(
                    (item for item in self.service.profile_list() if item.get("profile_id") == profile_id),
                    None,
                )
                if profile is None:
                    self._send({"error": "方案不存在"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send(profile)
            elif path in {"/api/training", "/api/training/results", "/api/training/scan"}:
                self._send({"results": self.service.training_results()})
            elif path == "/api/training/tasks":
                self._send({"tasks": self.service.training_tasks()})
            elif path.startswith("/api/runs/"):
                run_id = path.rsplit("/", 1)[-1]
                query = parse_qs(parsed.query)
                offset = int(query.get("offset", ["0"])[0])
                self._send(self.service.run_snapshot(run_id, offset))
            else:
                self._send({"error": "未找到接口"}, HTTPStatus.NOT_FOUND)
        except KeyError:
            self._send({"error": "运行记录不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            payload = self._read_json()
            if path in {"/api/generate", "/api/command"}:
                self._send(self.service.generate(payload))
            elif path == "/api/profiles":
                self._send(self.service.save_profile(payload), HTTPStatus.CREATED)
            elif path == "/api/execute":
                self._send(self.service.execute(payload), HTTPStatus.ACCEPTED)
            elif path == "/api/training/analyze":
                self._send(self.service.analyze_training(payload))
            elif path == "/api/training/log":
                self._send(self.service.training_log(payload))
            elif path == "/api/training/task-log":
                self._send(self.service.training_task_log(payload))
            elif path == "/api/strategy/params":
                self._send(self.service.strategy_params(payload))
            elif path == "/api/training/select":
                self._send(self.service.select_training_result(payload, selected=True))
            elif path == "/api/training/unselect":
                self._send(self.service.select_training_result(payload, selected=False))
            elif path.startswith("/api/runs/") and path.endswith("/stop"):
                run_id = path.split("/")[-2]
                self._send({"stopped": self.service.stop(run_id)})
            else:
                self._send({"error": "未找到接口"}, HTTPStatus.NOT_FOUND)
        except RuntimeError as exc:
            self._send({"error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            self._send({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path.startswith("/api/profiles/"):
            profile_id = path.rsplit("/", 1)[-1]
            self._send({"deleted": self.service.profile_store.delete(profile_id)})
            return
        self._send({"error": "未找到接口"}, HTTPStatus.NOT_FOUND)


class CommandCenterHTTPServer(ThreadingHTTPServer):
    """绑定服务对象的线程化 HTTP Server。"""

    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: CommandCenterService) -> None:
        self.service = service
        super().__init__(address, _RequestHandler)


def serve(
    project_root: Path | str | None = None,
    *,
    source_root: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> CommandCenterHTTPServer:
    """启动服务并可选地打开默认浏览器；返回 server 供调用方阻塞或关闭。"""

    service = CommandCenterService(project_root, source_root=source_root)
    server = CommandCenterHTTPServer((host, int(port)), service)
    address = server.server_address
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else address[0]
    url = f"http://{browser_host}:{address[1]}/"
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    print(f"QuantAda 命令工作台已启动: {url}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("[CommandCenter Warning] 工作台已监听非本机地址；该内部工具不提供认证，请仅在受信任网络使用。")
    return server


def main(project_root: Path | str | None = None, argv: list[str] | None = None) -> int:
    """启动本地网页工作台并持续服务。"""

    parser = argparse.ArgumentParser(description="QuantAda 本地 Web 命令工作台")
    parser.add_argument("--host", "--ui_ip", dest="host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", "--ui_port", dest="port", type=int, default=8765, help="监听端口，0 表示自动分配")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开默认浏览器")
    parser.add_argument(
        "--source-root",
        "--strategy-root",
        dest="source_root",
        default=None,
        help="外部策略/选股器源码仓库根目录",
    )
    args = parser.parse_args(argv)
    try:
        server = serve(
            project_root,
            source_root=args.source_root,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except OSError as exc:
        print(f"无法启动 QuantAda Web 工作台：{exc}")
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
