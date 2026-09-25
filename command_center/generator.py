"""把结构化命令预设编译成 run.py 参数和可复制文本。"""

from __future__ import annotations

import ast
import os
import platform
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from optimizer.journal_metadata import parse_recorded_value
from optimizer.training_tasks import RESUME_OPTIONS, build_resume_arguments

from .catalog import (
    CommandCatalog,
    CommandPreset,
    EnvRef,
    _PROVIDER_CREDENTIALS,
    _env,
    compose_gm_token,
    expand_gm_token_variables,
    parse_gm_token,
)


class MissingEnvironmentError(ValueError):
    """命令所需的环境变量为空。"""

    def __init__(self, names: list[str]) -> None:
        self.names = tuple(dict.fromkeys(names))
        super().__init__(f"缺少环境变量: {', '.join(self.names)}")


@dataclass(frozen=True)
class GeneratedCommand:
    """编译后的命令及其配置快照。"""

    argv: tuple[str, ...]
    variables: Mapping[str, str]
    config: Mapping[str, Any]
    params: Mapping[str, Any]
    options: Mapping[str, Any]
    warnings: tuple[str, ...] = ()


def build_resume_command(task, variables, project_root, source_root=None):
    """恢复选中的历史训练，不用当前表单参数或档案默认值改写原评分配置。"""
    arguments = build_resume_arguments(task)
    environ = {str(key): str(value) for key, value in variables.items()}
    if source_root:
        pythonpath = environ.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
        environ["PYTHONPATH"] = os.pathsep.join(item for item in (str(source_root), pythonpath) if item)
    # 续传使用普通文件锁；保留在工作台管理的子进程中，以便输出跟随和停止。
    environ.update(QUANTADA_DISABLE_AUTO_ELEVATE="1", QUANTADA_STUDY_NAME="", QUANTADA_STUDY_JOURNAL="")
    attrs = task["recorded"]
    options = {key: parse_recorded_value(attrs[key]) for key in RESUME_OPTIONS if key in attrs}
    options.update(
        metric=",".join(task["metrics"]), study_journal=task["journal"], n_trials=task["n_trials"],
        opt_params=parse_recorded_value(attrs["opt_params"]), risk_params=parse_recorded_value(attrs["risk_params"]),
        no_plot=bool(parse_recorded_value(attrs.get("no_plot", "True"))),
    )
    if "--study_name" in arguments:
        options["study_name"] = arguments[arguments.index("--study_name") + 1]
    return GeneratedCommand(
        tuple([environ.get("PYTHON_EXECUTABLE") or sys.executable, "-u", str(Path(project_root) / "run.py"), *arguments]),
        environ, parse_recorded_value(attrs["config"]), parse_recorded_value(attrs["params"]), options,
        warnings=(task["notice"],) if task.get("notice") else (),
    )


def command_response(generated, shell, platform_name):
    """把普通命令和续传命令统一转换为工作台预览及执行所需的数据。"""
    variables = {str(key): str(value) for key, value in generated.variables.items()}
    return {
        "argv": list(generated.argv), "variables": variables,
        "params": dict(generated.params), "config": dict(generated.config), "options": dict(generated.options),
        "warnings": list(generated.warnings), "command": render_shell_command(generated.argv, shell, platform_name),
        "display_command": render_portable_linux_display_command(generated.argv),
        "variables_text": render_variables(variables, shell), "shell": shell, "platform": platform_name,
    }


_OPTION_ORDER = (
    "selection",
    "data_source",
    "symbols",
    "cash",
    "commission",
    "slippage",
    "start_date",
    "end_date",
    "risk",
    "risk_params",
    "timeframe",
    "compression",
    "desc",
    "plot_scope",
    "refresh",
    "no_plot",
    "opt_params",
    "opt_schedule",
    "n_trials",
    "n_jobs",
    "metric",
    "train_roll_period",
    "test_roll_period",
    "train_ratio",
    "train_period",
    "test_period",
)


def detect_platform() -> dict[str, str]:
    """返回平台、解释器和推荐 Shell，供界面展示。"""

    system = platform.system() or "Unknown"
    if system == "Windows":
        shell = "PowerShell"
    elif system in {"Linux", "Darwin"}:
        shell = "Bash"
    else:
        shell = system
    return {
        "system": system,
        "release": platform.release(),
        "python": sys.executable,
        "shell": shell,
    }


def _resolve(value: Any, variables: Mapping[str, str], missing: list[str]) -> Any:
    if isinstance(value, EnvRef):
        resolved = str(variables.get(value.name, ""))
        if value.required and not resolved:
            missing.append(value.name)
        if not resolved:
            return resolved
        if value.value_type == "int":
            try:
                return int(resolved)
            except ValueError:
                missing.append(f"{value.name}（应为整数）")
        elif value.value_type in {"float", "number"}:
            try:
                number = float(resolved)
                return int(number) if value.value_type == "number" and number.is_integer() else number
            except ValueError:
                missing.append(f"{value.name}（应为数字）")
        return resolved
    if isinstance(value, Mapping):
        return {
            key: _resolve(item, variables, missing)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_resolve(item, variables, missing) for item in value)
    if isinstance(value, list):
        return [_resolve(item, variables, missing) for item in value]
    return value


def _append_option(argv: list[str], name: str, value: Any) -> None:
    if value is None or value is False:
        return
    flag = f"--{name}"
    if value is True:
        argv.append(flag)
    elif name == "symbols" and isinstance(value, (str, list, tuple)):
        # run.py 的 symbols 参数是逗号分隔字符串；同时接受界面提示的
        # ``['SHSE.600000', 'US.AAPL']`` 字面量，避免把括号当作代码名。
        values = value
        if isinstance(value, str) and value.strip().startswith(("[", "(")):
            try:
                values = ast.literal_eval(value)
            except (SyntaxError, ValueError, TypeError):
                values = value
        if isinstance(values, (list, tuple)):
            argv.extend((flag, ",".join(str(item).strip() for item in values)))
        else:
            argv.extend((flag, str(values)))
    elif isinstance(value, (Mapping, list, tuple)):
        argv.extend((flag, repr(value)))
    else:
        argv.extend((flag, str(value)))


def _uses_gm(preset: CommandPreset, raw_config: Mapping[str, Any]) -> bool:
    """当前命令是否需要写入 GM_TOKEN 配置。"""

    sources = re.split(r"[,\s]+", str(preset.data_source or "").strip().lower())
    connect = str(preset.connect or "").lower()
    return "gm" in sources or "gm_broker" in connect or "GM_TOKEN" in raw_config


def _apply_gm_token_config(
    config: dict[str, Any],
    variables: Mapping[str, str],
    missing: list[str],
    *,
    required: bool,
    default_host: str = "",
    default_port: str = "",
) -> None:
    """把三个 GM 输入聚合成 GM_TOKEN，并同步券商连接里的 token/地址。"""

    token = str(variables.get("GM_TOKEN") or "").strip()
    parsed_host = parsed_port = ""
    if "|" in token:
        token, parsed_host, parsed_port = parse_gm_token(token)
    host = str(variables.get("GM_HOST") or parsed_host or "").strip()
    port = str(variables.get("GM_PORT") or parsed_port or "").strip()
    # 方案连接中的 serv_addr 是明确配置。目录默认的 GM_HOST/GM_PORT 不能
    # 覆盖服务器方案；只有用户显式提供不同值（或 token 自带地址）才覆盖。
    profile_host = profile_port = ""
    brokers = config.get("BROKER_ENVIRONMENTS")
    if isinstance(brokers, dict):
        gm = brokers.get("gm_broker")
        if isinstance(gm, dict):
            for env in gm.values():
                if not isinstance(env, dict):
                    continue
                serv_addr = str(env.get("serv_addr") or "").strip()
                if not serv_addr:
                    continue
                profile_host, separator, profile_port = serv_addr.rpartition(":")
                if not separator:
                    profile_host, profile_port = serv_addr, ""
                profile_host, profile_port = profile_host.strip(), profile_port.strip()
                if profile_host:
                    break
    if not parsed_host and profile_host and host == str(default_host).strip():
        host = profile_host
    if not parsed_port and profile_port and port == str(default_port).strip():
        port = profile_port
    composed = compose_gm_token(token, host, port)
    if required and not token and "GM_TOKEN" not in missing:
        missing.append("GM_TOKEN")
    if required and token:
        if not host and "GM_HOST" not in missing:
            missing.append("GM_HOST")
        if not port and "GM_PORT" not in missing:
            missing.append("GM_PORT")
    if required or composed:
        config["GM_TOKEN"] = composed
    serv_addr = f"{host}:{port}" if host and port else ""
    brokers = config.get("BROKER_ENVIRONMENTS")
    if not isinstance(brokers, dict):
        return
    gm = brokers.get("gm_broker")
    if not isinstance(gm, dict):
        return
    for env in gm.values():
        if not isinstance(env, dict):
            continue
        if composed:
            env["token"] = composed
        if serv_addr:
            env["serv_addr"] = serv_addr


def build_command(
    catalog: CommandCatalog,
    preset: CommandPreset,
    variables: Mapping[str, str],
    project_root: Path | str,
    *,
    strict: bool = False,
) -> GeneratedCommand:
    """根据预设生成不依赖 Shell 的参数数组。"""

    missing: list[str] = []
    if not preset.strategy.strip():
        raise ValueError("策略不能为空")
    variables = expand_gm_token_variables(dict(variables), variables)
    profile = catalog.profile(preset.config_profile)
    raw_config: dict[str, Any] = {}
    if profile:
        raw_config.update(profile.config)
    raw_config.update(preset.config)
    # Provider 凭据属于配置而非子进程环境变量；自定义命令没有环境档案时，
    # 也要把已选择 Provider 的凭据映射到 config.py。
    compact_source = re.sub(
        r"\s*\+\s*", "+", str(preset.data_source or "").strip().lower()
    )
    source_names = re.split(r"[,\s]+", compact_source) if compact_source else []
    if preset.data_source:
        for source_name in source_names:
            credential_name = _PROVIDER_CREDENTIALS.get(
                "theta"
                if source_name in {"hybrid", "theta+futu", "futu+theta"}
                else source_name
            )
            if credential_name:
                raw_config.setdefault(credential_name, _env(credential_name))
    uses_futu = any(
        name in {"futu", "hybrid", "theta+futu", "futu+theta"}
        for name in source_names
    ) or "futu_broker" in str(preset.connect or "").lower()
    if uses_futu:
        raw_config.setdefault("FUTU_HOST", _env("FUTU_HOST"))
        raw_config.setdefault("FUTU_PORT", _env("FUTU_PORT", value_type="int"))
        raw_config.setdefault("FUTU_RSA_KEY_PATH", _env("FUTU_RSA_KEY_PATH", required=False))
        raw_config.setdefault("FUTU_TRADE_PASSWORD_ENV", _env("FUTU_TRADE_PASSWORD_ENV", required=False))
        raw_config.setdefault("FUTU_TRADE_PASSWORD_MD5_ENV", _env("FUTU_TRADE_PASSWORD_MD5_ENV", required=False))
        raw_config.setdefault("FUTU_ACCOUNT_ID", _env("FUTU_ACCOUNT_ID", required=False, value_type="int"))
        raw_config.setdefault("FUTU_ACCOUNT_INDEX", _env("FUTU_ACCOUNT_INDEX", required=False, value_type="int"))
        raw_config.setdefault("FUTU_ACCOUNT_CURRENCY", _env("FUTU_ACCOUNT_CURRENCY", required=False))
    # 自定义命令没有环境档案时，通知变量也必须能进入 config.py；空值不写入，
    # 这样不会把仓库默认配置意外覆盖成空字符串。
    for config_name in ("DINGTALK_WEBHOOK", "DINGTALK_SECRET", "WECOM_WEBHOOK"):
        if config_name not in raw_config and str(variables.get(config_name, "")).strip():
            raw_config[config_name] = _env(config_name, required=False)
    uses_gm = _uses_gm(preset, raw_config)
    if uses_gm:
        raw_config.setdefault("GM_TOKEN", _env("GM_TOKEN"))
    resolve_vars = dict(variables)
    composed = compose_gm_token(
        resolve_vars.get("GM_TOKEN", ""),
        resolve_vars.get("GM_HOST", ""),
        resolve_vars.get("GM_PORT", ""),
    )
    if composed:
        resolve_vars["GM_TOKEN"] = composed
    config = _resolve(raw_config, resolve_vars, missing)
    # 私有命令工作台可直接注入本机方案保存的 Futu 解锁凭据；空值不覆盖默认配置。
    if any(
        marker in f"{preset.data_source or ''} {preset.connect or ''} {preset.config_profile or ''}".lower()
        for marker in ("futu", "theta_futu")
    ):
        for name in ("FUTU_TRADE_PASSWORD", "FUTU_TRADE_PASSWORD_MD5"):
            value = str(variables.get(name) or "")
            if value:
                config[name] = value
    params = _resolve(dict(preset.params), resolve_vars, missing)
    options = _resolve(dict(preset.options), resolve_vars, missing)
    if uses_gm:
        variable_defaults = {
            item.name: str(item.default)
            for item in getattr(catalog, "variables", ())
        }
        _apply_gm_token_config(
            config,
            variables,
            missing,
            required=True,
            default_host=variable_defaults.get("GM_HOST", ""),
            default_port=variable_defaults.get("GM_PORT", ""),
        )

    run_path = Path(project_root).resolve() / "run.py"
    argv: list[str] = [str(variables.get("PYTHON_EXECUTABLE") or sys.executable), "-u", str(run_path)]
    argv.append(preset.strategy)
    if preset.selection:
        argv.extend(("--selection", preset.selection))
    if preset.data_source:
        argv.extend(("--data_source", preset.data_source))
    argv.extend(("--params", repr(params)))
    if config:
        argv.extend(("--config", repr(config)))
    if preset.connect:
        argv.extend(("--connect", preset.connect))
    for name in _OPTION_ORDER:
        if name in options:
            _append_option(argv, name, options[name])
    warnings_list = [f"未设置环境变量: {name}" for name in dict.fromkeys(missing)]
    unknown_options = sorted(set(options) - set(_OPTION_ORDER))
    warnings_list.extend(f"未识别运行选项（已忽略）: {name}" for name in unknown_options)
    warnings = tuple(warnings_list)
    if strict and missing:
        raise MissingEnvironmentError(missing)
    return GeneratedCommand(
        tuple(argv), dict(variables), config, params, options, warnings
    )


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _convert_wsl_paths(argv: tuple[str, ...] | list[str], platform_name: str | None) -> list[str]:
    """将 Windows 解释器和 run.py 路径转换为 WSL 的 /mnt 路径。"""

    if not platform_name or platform_name.lower() != "wsl":
        return [str(item) for item in argv]
    converted = [str(item) for item in argv]
    path_indexes = {0}
    path_indexes.update(
        index for index, item in enumerate(converted) if str(item).lower().replace("\\", "/").endswith("/run.py")
    )
    for index in path_indexes:
        value = converted[index]
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
        if match:
            suffix = match.group(2).replace("\\", "/")
            converted[index] = f"/mnt/{match.group(1).lower()}/{suffix}"
    return converted


def render_shell_command(
    argv: tuple[str, ...] | list[str],
    shell: str,
    platform_name: str | None = None,
) -> str:
    """把 argv 渲染成 Bash 或 PowerShell 可复制命令。"""

    normalized = shell.lower()
    bash_shells = {"bash", "sh", "zsh", "linux", "macos"}
    rendered_argv = _convert_wsl_paths(argv, platform_name if normalized in bash_shells else None)
    if normalized in bash_shells:
        return shlex.join(rendered_argv)
    if normalized in {"powershell", "pwsh", "windows", "ps"}:
        if not rendered_argv:
            return ""
        quoted = [_powershell_quote(str(item)) for item in rendered_argv]
        return "& " + " ".join(quoted)
    return " ".join(shlex.quote(str(item)) for item in rendered_argv)


def render_display_command(
    argv: tuple[str, ...] | list[str],
    shell: str,
    platform_name: str | None = None,
) -> str:
    """生成适合界面阅读的多行命令；复制时仍使用单行命令。"""

    normalized = shell.lower()
    bash_shells = {"bash", "sh", "zsh", "linux", "macos"}
    rendered_argv = _convert_wsl_paths(argv, platform_name if normalized in bash_shells else None)
    if not rendered_argv:
        return ""
    if normalized in bash_shells:
        parts = [shlex.quote(item) for item in rendered_argv]
        return parts[0] + " \\\n  " + " \\\n  ".join(parts[1:])
    if normalized in {"powershell", "pwsh", "windows", "ps"}:
        parts = [_powershell_quote(item) for item in rendered_argv]
        return "& " + parts[0] + " `\n  " + " `\n  ".join(parts[1:])
    return " \\\n  ".join(shlex.quote(item) for item in rendered_argv)


def portable_linux_argv(argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """把本机生成的 argv 转成可复制到 Linux 项目目录执行的参数数组。"""

    values = [str(item) for item in argv]
    run_index = next(
        (
            index
            for index, value in enumerate(values)
            if value.replace("\\", "/").rsplit("/", 1)[-1].lower() == "run.py"
        ),
        None,
    )
    if run_index is None:
        return ("python", "run.py", *values)
    return ("python", "run.py", *values[run_index + 1 :])


def _portable_bash_quote(value: str) -> str:
    """为可迁移预览选择较易读且不触发 Shell 展开的 Bash 引号。"""

    text = str(value)
    if text and re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", text):
        return text
    if text and "'" in text and '"' not in text and "\n" not in text:
        escaped = text.replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`")
        return f'"{escaped}"'
    return shlex.quote(text)


def render_portable_linux_display_command(argv: tuple[str, ...] | list[str]) -> str:
    """生成首行使用 ``python run.py`` 的 Linux 服务器多行命令。"""

    values = list(portable_linux_argv(argv))
    if len(values) <= 2:
        return shlex.join(values)
    parts = [_portable_bash_quote(item) for item in values]
    return parts[0] + " " + parts[1] + " \\\n  " + " \\\n  ".join(parts[2:])


def render_variables(variables: Mapping[str, str], shell: str) -> str:
    """生成全部变量的 Shell 设置文本。"""

    normalized = shell.lower()
    rows: list[str] = []
    for name, value in variables.items():
        if normalized in {"powershell", "pwsh", "windows", "ps"}:
            rows.append(f"$env:{name} = {_powershell_quote(str(value))}")
        else:
            rows.append(f"export {name}={shlex.quote(str(value))}")
    return "\n".join(rows)


def render_bundle(
    generated: GeneratedCommand,
    shell: str,
    result: str = "",
    platform_name: str | None = None,
) -> str:
    """生成变量、参数、命令和结果的完整复制文本。"""

    sections = [
        "# 环境变量",
        render_variables(
            {name: value for name, value in generated.variables.items() if name != "PYTHON_EXECUTABLE"},
            shell,
        ),
        "# 参数",
        repr(dict(generated.params)),
        "# 运行选项",
        repr(dict(generated.options)),
        "# 配置",
        repr(dict(generated.config)),
        "# 命令",
        render_shell_command(generated.argv, shell, platform_name),
    ]
    if result:
        sections.extend(("# 执行结果", result))
    return "\n".join(sections)
