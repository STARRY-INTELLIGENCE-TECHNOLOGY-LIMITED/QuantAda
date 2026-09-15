"""命令工作台的预设、环境变量和参数目录。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class EnvRef:
    """引用当前进程中的环境变量。"""

    name: str
    required: bool = True
    value_type: str = "str"


@dataclass(frozen=True)
class VariableSpec:
    """工作台可展示和编辑的变量定义。"""

    name: str
    default: str = ""
    description: str = ""


@dataclass(frozen=True)
class EnvironmentProfile:
    """一组可复用的运行配置。"""

    profile_id: str
    title: str
    config: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class CommandPreset:
    """一个可直接生成 run.py 命令的预设。"""

    preset_id: str
    title: str
    market: str
    mode: str
    strategy: str
    selection: str | None = None
    data_source: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    config_profile: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    connect: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""
    origin: str = "私有命令集"

    @property
    def requires_confirmation(self) -> bool:
        """实盘或明确连接配置时要求确认。"""

        return self.mode == "live" or bool(self.connect)


class CommandCatalog:
    """提供变量、环境配置和命令预设的统一访问入口。"""

    def __init__(
        self,
        variables: tuple[VariableSpec, ...],
        profiles: tuple[EnvironmentProfile, ...],
        presets: tuple[CommandPreset, ...],
    ) -> None:
        self.variables = variables
        self.profiles = profiles
        self.presets = presets
        self._profiles = {item.profile_id: item for item in profiles}
        self._presets = {item.preset_id: item for item in presets}

    def profile(self, profile_id: str | None) -> EnvironmentProfile | None:
        """按 ID 返回环境配置。"""

        return self._profiles.get(profile_id or "")

    def preset(self, preset_id: str) -> CommandPreset:
        """按 ID 返回命令预设。"""

        return self._presets[preset_id]

    def variable_values(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """读取变量快照：目录默认值 < 项目 config.py < 环境变量/命令集别名。"""

        values = {item.name: str(item.default) for item in self.variables}
        if environ is None:
            _fill_from_project_config(values)
            _fill_placeholders_from_source(values, _load_command_set_variables())
            source: Mapping[str, str] = os.environ
        else:
            source = environ
        _fill_from_source(values, source)
        return expand_gm_token_variables(values, source)


def _env(name: str, required: bool = True, value_type: str = "str") -> EnvRef:
    return EnvRef(name, required=required, value_type=value_type)


_PROVIDER_CREDENTIALS = {
    "tiingo": "TIINGO_TOKEN",
    "theta": "THETADATA_TOKEN",
    "thetadata": "THETADATA_TOKEN",
    "tushare": "TUSHARE_TOKEN",
    "sxsc_tushare": "SXSC_TUSHARE_TOKEN",
}


_VARIABLE_ALIASES = {
    "GM_TOKEN": ("GM_TOKEN", "GM_TOKEN_LOCAL", "GM_TOKEN_SERVER"),
    "WECOM_WEBHOOK": ("WECOM_WEBHOOK", "GLOBAL_WECOM_WEBHOOK"),
}


def _is_blank_or_placeholder(value: str) -> bool:
    """空值和框架占位符不用于反显。"""

    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    return lowered in {"your_token_here", "xxx", "changeme"} or lowered.startswith("your_token_here")


def _fill_from_source(values: dict[str, str], source: Mapping[str, str]) -> None:
    """用环境变量或调用方传入的映射覆盖当前值；支持命令集别名。"""

    for name in list(values):
        for key in _VARIABLE_ALIASES.get(name, (name,)):
            if key not in source:
                continue
            text = str(source.get(key, "")).strip()
            if text and not _is_blank_or_placeholder(text):
                values[name] = text
                break



def _command_set_candidate_paths() -> tuple[Path, ...]:
    """可选的本地命令集路径；存在才读取，不把密钥写入仓库。"""

    paths: list[Path] = []
    extra = str(os.environ.get("QUANTADA_COMMAND_SET") or "").strip()
    if extra:
        paths.append(Path(extra))
    paths.append(Path(__file__).resolve().parent / "local_command_set.ps1")
    return tuple(paths)


def _parse_command_set_exports(text: str) -> dict[str, str]:
    """解析 ``export NAME='value'`` 字面量；含未展开变量的值会被忽略。"""

    result: dict[str, str] = {}
    pattern = re.compile(
        r"^\s*export\s+([A-Z][A-Z0-9_]*)=(?:'([^']*)'|\"([^\"]*)\")",
        re.MULTILINE,
    )
    home = str(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "")
    for match in pattern.finditer(text):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else str(match.group(3) or "")
        if home:
            value = value.replace("$HOME", home).replace("${HOME}", home)
        if "$" in value:
            continue
        result[name] = value
    return result


def _load_command_set_variables() -> dict[str, str]:
    """按候选路径顺序回填命令集变量，先出现的键优先。"""

    merged: dict[str, str] = {}
    for path in _command_set_candidate_paths():
        try:
            if not path.is_file():
                continue
            parsed = _parse_command_set_exports(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        for key, value in parsed.items():
            merged.setdefault(key, value)
    return merged


def _fill_placeholders_from_source(values: dict[str, str], source: Mapping[str, str]) -> None:
    """只回填当前仍为空或占位符的变量，保留目录里已有的具体默认值。"""

    for name, current in list(values.items()):
        if not _is_blank_or_placeholder(current):
            continue
        for key in _VARIABLE_ALIASES.get(name, (name,)):
            if key not in source:
                continue
            text = str(source.get(key, "")).strip()
            if text and not _is_blank_or_placeholder(text):
                values[name] = text
                break


def _fill_from_project_config(values: dict[str, str]) -> None:
    """用项目 config.py 反显非占位符凭据，不覆盖目录里已有的具体默认值。"""

    try:
        import config as project_config
    except Exception:
        return
    for name, current in list(values.items()):
        if not _is_blank_or_placeholder(current):
            continue
        if not hasattr(project_config, name):
            continue
        raw = getattr(project_config, name)
        if isinstance(raw, (dict, list, tuple, set)):
            continue
        text = "" if raw is None else str(raw).strip()
        if not _is_blank_or_placeholder(text):
            values[name] = text


def parse_gm_token(value: str) -> tuple[str, str, str]:
    """把 ``token|host:port`` 拆成 token、地址和端口；缺段时返回空字符串。"""

    text = str(value or "").strip()
    if not text:
        return "", "", ""
    token, separator, rest = text.partition("|")
    token = token.strip()
    rest = rest.strip()
    if not separator:
        return token, "", ""
    host, colon, port = rest.rpartition(":")
    if not colon:
        return token, rest, ""
    return token, host.strip(), port.strip()


def compose_gm_token(token: str, host: str = "", port: str = "") -> str:
    """把 token、地址和端口聚合成单一 ``GM_TOKEN`` 配置值。"""

    token = str(token or "").strip()
    host = str(host or "").strip()
    port = str(port or "").strip()
    if "|" in token:
        parsed_token, parsed_host, parsed_port = parse_gm_token(token)
        token = parsed_token
        host = host or parsed_host
        port = port or parsed_port
    if token and host and port:
        return f"{token}|{host}:{port}"
    if token and host:
        return f"{token}|{host}"
    return token


def expand_gm_token_variables(
    values: Mapping[str, str],
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """反显 GM 连接：已有 ``token|host:port`` 或旧 LOCAL/SERVER 变量都拆到三个输入。"""

    result = {str(key): str(value) for key, value in dict(values).items()}
    source = source or result
    raw_token = str(result.get("GM_TOKEN") or "").strip()
    if "|" in raw_token:
        token, host, port = parse_gm_token(raw_token)
        result["GM_TOKEN"] = token
        if host:
            result["GM_HOST"] = host
        if port:
            result["GM_PORT"] = port
        return result
    if raw_token:
        return result
    fallback = str(source.get("GM_TOKEN_LOCAL") or source.get("GM_TOKEN_SERVER") or "").strip()
    if not fallback:
        return result
    token, host, port = parse_gm_token(fallback)
    result["GM_TOKEN"] = token
    if host:
        result["GM_HOST"] = host
    if port:
        result["GM_PORT"] = port
    return result


def _gm_environment_profiles() -> tuple[EnvironmentProfile, ...]:
    gm_common = {"GM_TOKEN": _env("GM_TOKEN")}
    return (
        EnvironmentProfile("none", "不覆盖 config.py", {}, "使用仓库当前配置"),
        EnvironmentProfile("gm_local", "GM 本地", gm_common),
        EnvironmentProfile(
            "gm_sim",
            "GM 券商模拟盘",
            {
                **gm_common,
                "BROKER_ENVIRONMENTS": {
                    "gm_broker": {
                        "sim": {
                            "strategy_id": _env("GM_STRATEGY_ID"),
                            "token": _env("GM_TOKEN"),
                            "serv_addr": "127.0.0.1:7001",
                            "schedule": "1d:14:51:00",
                        }
                    }
                },
            },
        ),
        EnvironmentProfile(
            "gm_live_local",
            "GM 本地实盘",
            {
                **gm_common,
                "BROKER_ENVIRONMENTS": {
                    "gm_broker": {
                        "sim": {
                            "strategy_id": _env("GM_STRATEGY_ID"),
                            "token": _env("GM_TOKEN"),
                            "serv_addr": "127.0.0.1:7001",
                            "schedule": "1d:14:45:00",
                        }
                    }
                },
                "ALARMS_ENABLED": True,
                "WECOM_WEBHOOK": _env("WECOM_WEBHOOK", required=False),
                "DINGTALK_WEBHOOK": _env("DINGTALK_WEBHOOK", required=False),
                "DINGTALK_SECRET": _env("DINGTALK_SECRET", required=False),
            },
        ),
        EnvironmentProfile(
            "gm_alarm",
            "GM 本地报警",
            {
                **gm_common,
                "ALARMS_ENABLED": True,
                "WECOM_WEBHOOK": _env("WECOM_WEBHOOK", required=False),
                "DINGTALK_WEBHOOK": _env("DINGTALK_WEBHOOK", required=False),
                "DINGTALK_SECRET": _env("DINGTALK_SECRET", required=False),
                "PRINT_PLAN": True,
                "LOG": True,
            },
        ),
        EnvironmentProfile(
            "gm_server",
            "GM 服务器实盘",
            {
                "GM_TOKEN": _env("GM_TOKEN"),
                "BROKER_ENVIRONMENTS": {
                    "gm_broker": {
                        "real": {
                            "strategy_id": _env("GM_STRATEGY_ID"),
                            "token": _env("GM_TOKEN"),
                            "serv_addr": "127.0.0.1:7001",
                            "schedule": "1d:14:45:00",
                        }
                    }
                },
                "ALARMS_ENABLED": True,
                "WECOM_WEBHOOK": _env("WECOM_WEBHOOK", required=False),
                "DINGTALK_WEBHOOK": _env("DINGTALK_WEBHOOK", required=False),
                "DINGTALK_SECRET": _env("DINGTALK_SECRET", required=False),
                "PRINT_PLAN": True,
            },
        ),
        EnvironmentProfile(
            "futu",
            "Futu",
            {
                "FUTU_HOST": _env("FUTU_HOST"),
                "FUTU_PORT": _env("FUTU_PORT", value_type="int"),
                "FUTU_RSA_KEY_PATH": _env("FUTU_RSA_KEY_PATH", required=False),
                "FUTU_TRADE_PASSWORD_ENV": _env("FUTU_TRADE_PASSWORD_ENV", required=False),
                "FUTU_TRADE_PASSWORD_MD5_ENV": _env("FUTU_TRADE_PASSWORD_MD5_ENV", required=False),
                "FUTU_ACCOUNT_ID": _env("FUTU_ACCOUNT_ID", required=False, value_type="int"),
                "FUTU_ACCOUNT_INDEX": _env("FUTU_ACCOUNT_INDEX", required=False, value_type="int"),
                "FUTU_ACCOUNT_CURRENCY": _env("FUTU_ACCOUNT_CURRENCY", required=False),
                "LOT_SIZE": _env("CN_LOT_SIZE", value_type="int"),
            },
        ),
        EnvironmentProfile(
            "futu_global",
            "Futu 全球",
            {
                "FUTU_HOST": _env("FUTU_HOST"),
                "FUTU_PORT": _env("FUTU_PORT", value_type="int"),
                "FUTU_RSA_KEY_PATH": _env("FUTU_RSA_KEY_PATH", required=False),
                "FUTU_TRADE_PASSWORD_ENV": _env("FUTU_TRADE_PASSWORD_ENV", required=False),
                "FUTU_TRADE_PASSWORD_MD5_ENV": _env("FUTU_TRADE_PASSWORD_MD5_ENV", required=False),
                "FUTU_ACCOUNT_ID": _env("FUTU_ACCOUNT_ID", required=False, value_type="int"),
                "FUTU_ACCOUNT_INDEX": _env("FUTU_ACCOUNT_INDEX", required=False, value_type="int"),
                "FUTU_ACCOUNT_CURRENCY": _env("FUTU_ACCOUNT_CURRENCY", required=False),
                "LOT_SIZE": _env("GLOBAL_LOT_SIZE", value_type="number"),
            },
        ),
        EnvironmentProfile(
            "theta_futu_global",
            "ThetaData + Futu 全球混合",
            {
                "THETADATA_TOKEN": _env("THETADATA_TOKEN"),
                "FUTU_HOST": _env("FUTU_HOST"),
                "FUTU_PORT": _env("FUTU_PORT", value_type="int"),
                "FUTU_RSA_KEY_PATH": _env("FUTU_RSA_KEY_PATH", required=False),
                "FUTU_TRADE_PASSWORD_ENV": _env(
                    "FUTU_TRADE_PASSWORD_ENV", required=False
                ),
                "FUTU_TRADE_PASSWORD_MD5_ENV": _env(
                    "FUTU_TRADE_PASSWORD_MD5_ENV", required=False
                ),
                "FUTU_ACCOUNT_ID": _env("FUTU_ACCOUNT_ID", required=False, value_type="int"),
                "FUTU_ACCOUNT_INDEX": _env("FUTU_ACCOUNT_INDEX", required=False, value_type="int"),
                "FUTU_ACCOUNT_CURRENCY": _env("FUTU_ACCOUNT_CURRENCY", required=False),
                "LOT_SIZE": _env("GLOBAL_LOT_SIZE", value_type="number"),
            },
            "历史风险字段使用 ThetaData，实盘当前行情使用 Futu；回测不会请求 Futu 实时快照。",
        ),
        EnvironmentProfile(
            "tiingo",
            "Tiingo",
            {
                "TIINGO_TOKEN": _env("TIINGO_TOKEN"),
                "LOT_SIZE": _env("GLOBAL_LOT_SIZE", value_type="number"),
                "IBKR_PORT": 4001,
                "IBKR_CLIENT_ID": 1000,
            },
        ),
        EnvironmentProfile(
            "theta",
            "ThetaData",
            {"THETADATA_TOKEN": _env("THETADATA_TOKEN")},
        ),
        EnvironmentProfile(
            "ib_personal",
            "IBKR 个人账户",
            {
                "TIINGO_TOKEN": _env("TIINGO_TOKEN"),
                "LOT_SIZE": _env("GLOBAL_LOT_SIZE", value_type="number"),
                "IBKR_HOST": _env("IBKR_HOST"),
                "IBKR_PORT": 4003,
                "IBKR_ORDER_ACCOUNT": _env("IBKR_ORDER_ACCOUNT"),
                "IBKR_CLIENT_ID": 0,
                "PRINT_PLAN": True,
                "BROKER_ENVIRONMENTS": {
                    "ib_broker": {
                        "real": {
                            "schedule": "1d:15:45:00",
                            "timezone": "America/New_York",
                        }
                    }
                },
                "ALARMS_ENABLED": True,
                "WECOM_WEBHOOK": _env("WECOM_WEBHOOK", required=False),
                "DINGTALK_WEBHOOK": _env("DINGTALK_WEBHOOK", required=False),
                "DINGTALK_SECRET": _env("DINGTALK_SECRET", required=False),
            },
        ),
        EnvironmentProfile(
            "ib_institution",
            "IBKR 机构账户",
            {
                "TIINGO_TOKEN": _env("TIINGO_TOKEN"),
                "LOT_SIZE": _env("GLOBAL_LOT_SIZE", value_type="number"),
                "IBKR_PORT": 5001,
                "IBKR_CLIENT_ID": 0,
                "PRINT_PLAN": True,
                "BROKER_ENVIRONMENTS": {
                    "ib_broker": {
                        "real_2": {
                            "schedule": "1d:15:45:00",
                            "timezone": "America/New_York",
                        }
                    }
                },
                "ALARMS_ENABLED": True,
                "WECOM_WEBHOOK": _env("WECOM_WEBHOOK", required=False),
                "DINGTALK_WEBHOOK": _env("DINGTALK_WEBHOOK", required=False),
                "DINGTALK_SECRET": _env("DINGTALK_SECRET", required=False),
            },
        ),
        EnvironmentProfile(
            "ib_sim",
            "IBKR 模拟",
            {
                "TIINGO_TOKEN": _env("TIINGO_TOKEN"),
                "LOT_SIZE": _env("GLOBAL_LOT_SIZE", value_type="number"),
                "IBKR_PORT": 4001,
                "IBKR_CLIENT_ID": 99,
                "PRINT_PLAN": True,
                "BROKER_ENVIRONMENTS": {
                    "ib_broker": {
                        "sim": {
                            "host": "127.0.0.1",
                            "port": 4002,
                            "client_id": 99,
                            "schedule": "1d:10:57:40",
                            "timezone": "America/New_York",
                        }
                    }
                },
            },
        ),
    )


def _readme_presets() -> tuple[CommandPreset, ...]:
    """创建 README 中公开示例命令对应的方案。"""

    return (
        CommandPreset(
            "readme_macd_tiingo",
            "MACD 示例 · Tiingo",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="tiingo",
            config_profile="tiingo",
            config={"PRINT_PLAN": True},
            options={"symbols": "SHSE.600519"},
            origin="README",
        ),
        CommandPreset(
            "readme_macd_basic",
            "MACD 示例 · 基础回测",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            options={"symbols": "SHSE.600519"},
            origin="README",
        ),
        CommandPreset(
            "readme_auto_symbols",
            "自动轮动示例 · 多标的",
            "示例",
            "backtest",
            "strategies.sample_auto_rebalance_strategy",
            options={
                "symbols": "SHSE.510300,SHSE.510500,SZSE.159915,SHSE.511880",
                "start_date": "20230101",
            },
            origin="README",
        ),
        CommandPreset(
            "readme_auto_selector",
            "自动轮动示例 · 选股器",
            "示例",
            "backtest",
            "strategies.sample_auto_rebalance_strategy",
            selection="stock_selectors.sample_manual_selector",
            options={"start_date": "20240101"},
            origin="README",
        ),
        CommandPreset(
            "readme_macd_risk",
            "MACD 示例 · 多风控",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            options={
                "symbols": "SHSE.600519",
                "risk": "risk_controls.sample_stop_loss_take_profit,risk_controls.sample_trend_protection",
            },
            origin="README",
        ),
        CommandPreset(
            "readme_auto_params",
            "自动轮动示例 · 参数与风控",
            "示例",
            "backtest",
            "strategies.sample_auto_rebalance_strategy",
            params={"selectTopK": 2, "roc_period": 10},
            options={
                "symbols": "SZSE.159915",
                "risk_params": {"stop_loss_pct": 0.05},
            },
            origin="README",
        ),
        CommandPreset(
            "readme_macd_csv",
            "MACD 示例 · CSV",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="csv",
            options={"symbols": "SHSE.600519"},
            origin="README",
        ),
        CommandPreset(
            "readme_macd_csv_refresh",
            "MACD 示例 · CSV 刷新",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="csv",
            options={"symbols": "SHSE.600519", "refresh": True},
            origin="README",
        ),
        CommandPreset(
            "readme_macd_theta",
            "MACD 示例 · ThetaData",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="theta",
            config_profile="theta",
            options={"symbols": "US.AAPL"},
            config={"CACHE_DATA": True},
            origin="README",
        ),
        CommandPreset(
            "readme_macd_theta_refresh",
            "MACD 示例 · ThetaData 刷新",
            "示例",
            "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="theta",
            config_profile="theta",
            options={"symbols": "US.AAPL", "refresh": True},
            config={"CACHE_DATA": True},
            origin="README",
        ),
        CommandPreset(
            "readme_macd_optimize",
            "MACD 示例 · 参数优化",
            "示例",
            "optimize",
            "strategies.sample_macd_cross_strategy",
            options={
                "symbols": "SHSE.600519",
                "opt_params": {"fast_period": {"type": "int", "low": 5, "high": 30}},
            },
            origin="README",
        ),
        CommandPreset(
            "readme_macd_train_test",
            "MACD 示例 · 训练测试",
            "示例",
            "optimize",
            "strategies.sample_macd_cross_strategy",
            options={
                "symbols": "SHSE.600519",
                "opt_params": {"fast_period": {"type": "int", "low": 5, "high": 30}},
                "train_period": "20210101-20221231",
                "test_period": "20230101-20231231",
                "n_trials": 50,
            },
            origin="README",
        ),
        CommandPreset(
            "readme_gm_sim",
            "GM 模拟连接示例",
            "示例",
            "live",
            "strategies.sample_auto_rebalance_strategy",
            data_source="gm",
            config_profile="gm_local",
            options={"symbols": "SHSE.510300", "no_plot": True},
            connect="gm_broker:sim",
            origin="README",
        ),
        CommandPreset(
            "readme_gm_real",
            "GM 实盘连接示例",
            "示例",
            "live",
            "strategies.sample_auto_rebalance_strategy",
            data_source="gm",
            config_profile="gm_local",
            options={"symbols": "SHSE.510300", "no_plot": True},
            connect="gm_broker:real",
            origin="README",
        ),
        CommandPreset(
            "readme_ib_sim",
            "IBKR 模拟连接示例",
            "示例",
            "live",
            "strategies.sample_auto_rebalance_strategy",
            data_source="tiingo",
            config_profile="ib_sim",
            options={"symbols": "US.AAPL", "no_plot": True},
            connect="ib_broker:sim",
            origin="README",
        ),
        CommandPreset(
            "readme_futu_sim",
            "Futu 模拟连接示例",
            "示例",
            "live",
            "strategies.sample_auto_rebalance_strategy",
            data_source="futu",
            config_profile="futu",
            options={"symbols": "HK.00700", "no_plot": True},
            connect="futu_broker:sim",
            origin="README",
        ),
        CommandPreset(
            "readme_futu_real",
            "Futu 实盘连接示例",
            "示例",
            "live",
            "strategies.sample_auto_rebalance_strategy",
            data_source="futu",
            config_profile="futu",
            options={"symbols": "HK.00700", "no_plot": True},
            connect="futu_broker:real",
            origin="README",
        ),
        CommandPreset(
            "readme_futu_event",
            "Futu 行情事件示例",
            "示例",
            "live",
            "strategies.sample_auto_rebalance_strategy",
            data_source="futu",
            config_profile="futu",
            options={"symbols": "SHSE.600519"},
            connect="futu_broker:real_event",
            origin="README",
        ),
    )


def _private_catalog_candidate_paths(project_root=None, source_root=None) -> tuple[Path, ...]:
    """返回本机私有目录配置候选路径；仓库默认值不包含私有索引。"""

    roots = [Path(project_root or Path.cwd()).resolve()]
    source_root = source_root or os.environ.get("QUANTADA_SOURCE_ROOT")
    if source_root:
        try:
            candidate_root = Path(source_root).expanduser().resolve()
        except OSError:
            candidate_root = Path(source_root).expanduser()
        if candidate_root not in roots:
            roots.append(candidate_root)
    candidates = []
    configured = str(os.environ.get("QUANTADA_PRIVATE_CATALOG") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    try:
        import config as project_config

        data_value = str(getattr(project_config, "DATA_PATH", ".data") or ".data")
    except Exception:
        data_value = ".data"
    for root in roots:
        data_root = Path(data_value).expanduser()
        if not data_root.is_absolute():
            data_root = root / data_root
        for base in (data_root / "command_center", data_root):
            for name in (
                "private_catalog.json",
                "catalog_private.json",
                "private_config.json",
                "private_command_set.json",
                "command_set.json",
                "private_commands.json",
            ):
                candidates.append(base / name)
    unique = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        marker = str(resolved).lower()
        if marker not in seen:
            seen.add(marker)
            unique.append(resolved)
    return tuple(unique)


def _load_private_catalog(project_root=None, source_root=None) -> dict[str, Any]:
    """读取第一个有效的本机私有目录配置。"""

    for path in _private_catalog_candidate_paths(project_root, source_root):
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("catalog"), Mapping):
            payload = payload["catalog"]
        if isinstance(payload, Mapping):
            return dict(payload)
        if isinstance(payload, list):
            return {"presets": payload}
    return {}


def _private_value(value):
    """提取私有变量的值；允许 value/default 两种简洁写法。"""

    if isinstance(value, Mapping):
        if "value" in value:
            return value["value"]
        if "default" in value:
            return value["default"]
    return value


def _private_variable_specs(public_specs, payload):
    """以私有变量覆盖公开默认值，并保留新增变量。"""

    raw = payload.get("variables")
    if not isinstance(raw, Mapping):
        raw = {
            str(name): value
            for name, value in payload.items()
            if isinstance(name, str) and name.isupper()
        }
    if not isinstance(raw, Mapping):
        return tuple(public_specs)
    overrides = {str(name): _private_value(value) for name, value in raw.items()}
    merged = []
    known = set()
    for item in public_specs:
        value = overrides.get(item.name, item.default)
        default = "" if value is None else str(value)
        merged.append(VariableSpec(item.name, default, item.description))
        known.add(item.name)
    for name, value in overrides.items():
        if name in known:
            continue
        default = "" if value is None else str(value)
        description = ""
        raw_item = raw.get(name)
        if isinstance(raw_item, Mapping):
            description = str(raw_item.get("description") or "")
        merged.append(VariableSpec(name, default, description))
    return tuple(merged)


def _private_profile(item, key=None):
    """将私有 JSON 配置转换为环境档案。"""

    if not isinstance(item, Mapping):
        return None
    profile_id = str(item.get("profile_id") or item.get("id") or key or "").strip()
    if not profile_id:
        return None
    config_value = item.get("config", item.get("config_override", {}))
    if not isinstance(config_value, Mapping):
        config_value = {}
    return EnvironmentProfile(
        profile_id=profile_id,
        title=str(item.get("title") or item.get("name") or profile_id),
        config=dict(config_value),
        description=str(item.get("description") or ""),
    )


def _private_preset(item, key=None):
    """将私有 JSON 配置转换为命令预设。"""

    if not isinstance(item, Mapping):
        return None
    preset_id = str(item.get("preset_id") or item.get("id") or key or "").strip()
    strategy = str(item.get("strategy") or "").strip()
    if not preset_id or not strategy:
        return None

    def mapping(name):
        value = item.get(name, {})
        return dict(value) if isinstance(value, Mapping) else {}

    return CommandPreset(
        preset_id=preset_id,
        title=str(item.get("title") or item.get("name") or preset_id),
        market=str(item.get("market") or "自定义"),
        mode=str(item.get("mode") or "backtest"),
        strategy=strategy,
        selection=str(item.get("selection") or "") or None,
        data_source=str(item.get("data_source") or "") or None,
        params=mapping("params"),
        config_profile=str(item.get("config_profile") or "") or None,
        config=(
            mapping("config")
            if isinstance(item.get("config"), Mapping)
            else mapping("config_override")
        ),
        connect=str(item.get("connect") or "") or None,
        options=mapping("options"),
        description=str(item.get("description") or ""),
        # 私有文件中的条目始终属于私有来源，避免误填 README 后被排到公开区。
        origin=(
            "私有命令集"
            if str(item.get("origin") or "").strip() in {"", "README"}
            else str(item.get("origin"))
        ),
    )


def _private_items(raw, converter):
    """兼容列表或以 ID 为键的私有项映射。"""

    if isinstance(raw, Mapping):
        return tuple(
            converted
            for key, value in raw.items()
            if (converted := converter(value, key)) is not None
        )
    if isinstance(raw, (list, tuple)):
        return tuple(
            converted
            for value in raw
            if (converted := converter(value)) is not None
        )
    return ()


def _private_strategy_index(raw):
    """把简化的策略索引映射转换为可展示的私有预设。"""

    if isinstance(raw, Mapping):
        items = []
        for key, value in raw.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("preset_id", str(key))
            else:
                item = {"preset_id": str(key), "title": str(key), "strategy": str(value)}
            items.append(item)
        return items
    if isinstance(raw, (list, tuple)):
        return [
            {"preset_id": f"private_strategy_{index}", "strategy": str(value)}
            for index, value in enumerate(raw, 1)
            if str(value or "").strip()
        ]
    return ()


def _merge_catalog_items(public_items, private_items):
    """按 ID 合并私有项；同 ID 覆盖，新增项追加。"""

    merged = list(public_items)
    positions = {
        item.profile_id if isinstance(item, EnvironmentProfile) else item.preset_id: index
        for index, item in enumerate(merged)
    }
    for item in private_items:
        item_id = item.profile_id if isinstance(item, EnvironmentProfile) else item.preset_id
        position = positions.get(item_id)
        if position is None:
            positions[item_id] = len(merged)
            merged.append(item)
        else:
            merged[position] = item
    return tuple(merged)


def default_catalog(project_root=None, source_root=None) -> CommandCatalog:
    """创建脱敏的公开目录，并把本机私有目录配置作为覆盖层合并。"""

    variables = (
        VariableSpec("FUTU_HOST", "", "Futu OpenD 地址"),
        VariableSpec("FUTU_PORT", "11111", "Futu OpenD 端口"),
        VariableSpec("FUTU_RSA_KEY_PATH", "", "Futu RSA 密钥路径；清空可关闭协议加密"),
        VariableSpec("FUTU_TRADE_PASSWORD_ENV", "", "Futu 实盘解锁密码所在环境变量名；不填写密码本身"),
        VariableSpec("FUTU_TRADE_PASSWORD_MD5_ENV", "", "Futu 实盘解锁 MD5 所在环境变量名；与密码环境变量二选一"),
        VariableSpec("FUTU_TRADE_PASSWORD", "", "Futu 实盘解锁密码"),
        VariableSpec("FUTU_TRADE_PASSWORD_MD5", "", "Futu 实盘解锁 MD5"),
        VariableSpec("FUTU_ACCOUNT_ID", "0", "Futu 账户 ID；0 表示默认账户"),
        VariableSpec("FUTU_ACCOUNT_INDEX", "0", "Futu 账户索引；0 表示默认账户"),
        VariableSpec("FUTU_ACCOUNT_CURRENCY", "HKD", "Futu 账户现金计价币种"),
        VariableSpec("GM_TOKEN", "", "GM token"),
        VariableSpec("GM_STRATEGY_ID", "", "GM 策略 ID"),
        VariableSpec("GM_HOST", "127.0.0.1", "GM 服务地址"),
        VariableSpec("GM_PORT", "7001", "GM 服务端口"),
        VariableSpec("WECOM_WEBHOOK", "", "企业微信 webhook"),
        VariableSpec("DINGTALK_WEBHOOK", "", "钉钉机器人 webhook"),
        VariableSpec("DINGTALK_SECRET", "", "钉钉机器人加签密钥（可选）"),
        VariableSpec("TUSHARE_TOKEN", "", "Tushare Pro token"),
        VariableSpec("TIINGO_TOKEN", "", "Tiingo token"),
        VariableSpec("THETADATA_TOKEN", "", "ThetaData token"),
        VariableSpec("SXSC_TUSHARE_TOKEN", "", "SXSC Tushare token"),
        VariableSpec("CN_LOT_SIZE", "100", "A 股最小交易单位"),
        VariableSpec("GLOBAL_LOT_SIZE", "1", "全球市场最小交易单位"),
        VariableSpec("IBKR_HOST", "", "IBKR 主机地址"),
        VariableSpec("IBKR_ORDER_ACCOUNT", "", "IBKR 下单账户"),
    )
    private = _load_private_catalog(project_root, source_root)
    variables = _private_variable_specs(variables, private)
    profiles = _merge_catalog_items(
        _gm_environment_profiles(),
        _private_items(private.get("profiles"), _private_profile),
    )
    private_presets = private.get("presets")
    if private_presets is None:
        private_presets = private.get("commands", private.get("command_set"))
    if private_presets is None:
        private_presets = _private_strategy_index(private.get("strategies"))
    presets = _merge_catalog_items(
        _readme_presets(),
        _private_items(private_presets, _private_preset),
    )
    return CommandCatalog(variables, profiles, presets)
