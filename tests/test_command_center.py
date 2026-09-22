"""命令工作台的模型、生成器和执行器测试。"""

from __future__ import annotations

import ast
import sys
import threading
import time
import os
from dataclasses import replace
from pathlib import Path

import pytest

import command_center.catalog as catalog_module
from command_center import (
    CommandPreset,
    MissingEnvironmentError,
    build_command,
    default_catalog,
    detect_platform,
    portable_linux_argv,
    render_portable_linux_display_command,
    render_shell_command,
    render_display_command,
    render_variables,
)
from command_center.executor import CommandExecutor


def _public_catalog(root: Path, monkeypatch: pytest.MonkeyPatch | None = None):
    """隔离本机私有目录后返回公开目录。私有层缺失时仍可验证公开行为。"""

    if monkeypatch is not None:
        monkeypatch.delenv("QUANTADA_PRIVATE_CATALOG", raising=False)
        monkeypatch.delenv("QUANTADA_SOURCE_ROOT", raising=False)
        monkeypatch.delenv("QUANTADA_COMMAND_SET", raising=False)
    return default_catalog(root / "without-private")


def _skip_unless_names(catalog, names: set[str], reason: str) -> None:
    """所需变量不存在时跳过，避免可选目录项缺失导致硬失败。"""

    present = {item.name for item in catalog.variables}
    if not names <= present:
        pytest.skip(reason)

def test_default_catalog_covers_main_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公开方案存在就验证主流程；本机私有方案存在则验证已合并，没有则不因此失败。"""

    catalog = default_catalog()
    public = [item for item in catalog.presets if item.origin == "README"]
    if not public:
        pytest.skip("没有 README 公开方案")
    assert len(public) >= 10
    sample = [item for item in public if item.market == "示例"]
    if not sample:
        pytest.skip("没有示例市场方案")
    assert all("strategies_custom" not in item.strategy for item in public)
    assert {item.mode for item in sample} == {"backtest", "live", "optimize"}
    for item in public:
        if item.market == "示例":
            continue
        assert item.strategy.strip()
        assert item.mode in {"backtest", "live", "optimize"}
    gm_sim = next((item for item in public if item.preset_id == "readme_gm_sim"), None)
    if gm_sim is None:
        pytest.skip("readme_gm_sim 不存在")
    assert gm_sim.strategy.startswith("strategies.sample_")
    _skip_unless_names(
        catalog,
        {"FUTU_TRADE_PASSWORD_ENV", "FUTU_TRADE_PASSWORD_MD5_ENV", "FUTU_RSA_KEY_PATH"},
        "Futu 变量未完整暴露",
    )
    if catalog.profile("theta_futu_global") is None:
        pytest.skip("theta_futu_global 不存在")
    public_catalog = _public_catalog(tmp_path, monkeypatch)
    assert public_catalog.variable_values({})["FUTU_RSA_KEY_PATH"] == ""
    private = [item for item in catalog.presets if item.origin != "README"]
    if not private:
        return
    assert all(item.origin == "私有命令集" for item in private)
    assert all(item.strategy.strip() for item in private)

def test_private_catalog_overrides_sanitized_defaults_and_adds_private_presets(tmp_path: Path) -> None:
    private_dir = tmp_path / ".data" / "command_center"
    private_dir.mkdir(parents=True)
    (private_dir / "private_catalog.json").write_text(
        """{
          "variables": {
            "FUTU_HOST": "private.opend.example",
            "IBKR_ORDER_ACCOUNT": "U_PRIVATE"
          },
          "profiles": {
            "private_env": {
              "title": "私有环境",
              "config_override": {"FUTU_HOST": "private.opend.example"}
            }
          },
          "presets": [
            {
              "preset_id": "private_strategy_v1",
              "title": "私有策略",
              "market": "全球",
              "mode": "backtest",
              "strategy": "private_strategies.alpha",
              "selection": "private_selectors.top",
              "data_source": "theta",
              "options": {"symbols": "US.AAPL"},
              "origin": "私有命令集"
            }
          ]
        }""",
        encoding="utf-8",
    )

    public = default_catalog(tmp_path / "without-private")
    merged = default_catalog(tmp_path)

    assert public.preset("readme_macd_basic").strategy.startswith("strategies.sample_")
    assert merged.variable_values({})["FUTU_HOST"] == "private.opend.example"
    assert merged.variable_values({})["IBKR_ORDER_ACCOUNT"] == "U_PRIVATE"
    private = merged.preset("private_strategy_v1")
    assert private.strategy == "private_strategies.alpha"
    assert private.selection == "private_selectors.top"
    assert merged.profile("private_env").config["FUTU_HOST"] == "private.opend.example"


def test_private_catalog_can_live_under_external_strategy_root(tmp_path: Path) -> None:
    private_dir = tmp_path / ".data"
    private_dir.mkdir(parents=True)
    (private_dir / "private_catalog.json").write_text(
        '{"presets": [{"id": "external-private", "strategy": "private.alpha"}]}',
        encoding="utf-8",
    )

    catalog = default_catalog(tmp_path / "public-project", tmp_path)

    assert catalog.preset("external-private").strategy == "private.alpha"


def test_private_catalog_accepts_flat_variables_and_strategy_index(tmp_path: Path) -> None:
    private_dir = tmp_path / ".data"
    private_dir.mkdir(parents=True)
    (private_dir / "private_config.json").write_text(
        '{"FUTU_HOST": "flat.opend.example", "strategies": {"alpha": "private.alpha"}}',
        encoding="utf-8",
    )

    catalog = default_catalog(tmp_path)

    assert catalog.variable_values({})["FUTU_HOST"] == "flat.opend.example"
    assert catalog.preset("alpha").strategy == "private.alpha"


def test_default_catalog_contains_no_private_strategy_or_endpoint_literals() -> None:
    catalog = default_catalog(Path(".nonexistent-private-catalog"))
    serialized = repr(catalog)
    assert "strategies_custom" not in serialized
    assert "stock_selectors_custom" not in serialized
    assert "private.opend.example" not in serialized
    assert "private_strategies" not in serialized


def test_catalog_does_not_expose_private_optimization_constants() -> None:
    """公开目录不得通过未使用的模块常量泄露私有优化结果或搜索空间。"""

    leaked_names = (
        "CN_PARAMS",
        "GLOBAL_PARAMS_V1",
        "GLOBAL_PARAMS_V2",
        "GLOBAL_US_PARAMS",
        "GLOBAL_IB_SIM_PARAMS",
        "OPTION_PARAMS",
        "CN_OPT_PARAMS_VERIFY",
        "CN_OPT_PARAMS_FULL",
        "GLOBAL_OPT_PARAMS_V1",
        "GLOBAL_OPT_PARAMS_V2",
    )
    assert all(not hasattr(catalog_module, name) for name in leaked_names)


def test_futu_catalog_exposes_account_routing_and_currency() -> None:
    catalog = default_catalog()
    names = {item.name for item in catalog.variables}
    assert {
        "FUTU_ACCOUNT_ID",
        "FUTU_ACCOUNT_INDEX",
        "FUTU_ACCOUNT_CURRENCY",
    } <= names
    generated = build_command(
        catalog,
        CommandPreset(
            "futu-account",
            "futu-account",
            "示例",
            "backtest",
            "strategies.sample_auto_rebalance_strategy",
            data_source="futu",
            config_profile="futu_global",
        ),
        catalog.variable_values({
            "FUTU_ACCOUNT_ID": "7",
            "FUTU_ACCOUNT_INDEX": "1",
            "FUTU_ACCOUNT_CURRENCY": "USD",
        }),
        Path("."),
    )
    config_value = generated.argv[generated.argv.index("--config") + 1]
    assert "'FUTU_ACCOUNT_ID': 7" in config_value
    assert "'FUTU_ACCOUNT_INDEX': 1" in config_value
    assert "'FUTU_ACCOUNT_CURRENCY': 'USD'" in config_value


def test_default_catalog_exposes_notification_and_provider_credentials() -> None:
    catalog = default_catalog()
    names = {item.name for item in catalog.variables}
    assert {
        "DINGTALK_WEBHOOK",
        "DINGTALK_SECRET",
        "WECOM_WEBHOOK",
        "TUSHARE_TOKEN",
        "TIINGO_TOKEN",
        "THETADATA_TOKEN",
    } <= names

    variables = catalog.variable_values(
        {"DINGTALK_WEBHOOK": "https://example.invalid/ding", "DINGTALK_SECRET": "secret"}
    )
    generated = build_command(catalog, catalog.preset("readme_gm_sim"), variables, Path("."))
    config_value = generated.argv[generated.argv.index("--config") + 1]
    assert "DINGTALK_WEBHOOK" in config_value
    assert "https://example.invalid/ding" in config_value
    assert "DINGTALK_SECRET" in config_value
    assert "GLOBAL_WECOM_WEBHOOK" not in names
    variable_order = [item.name for item in catalog.variables]
    assert variable_order.index("WECOM_WEBHOOK") < variable_order.index("DINGTALK_WEBHOOK")

    global_generated = build_command(
        catalog,
        CommandPreset(
            "ib-account", "ib-account", "示例", "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="tiingo", config_profile="ib_personal",
        ),
        catalog.variable_values({"WECOM_WEBHOOK": "https://example.invalid/wecom"}),
        Path("."),
    )
    global_config = global_generated.argv[global_generated.argv.index("--config") + 1]
    assert "'WECOM_WEBHOOK': 'https://example.invalid/wecom'" in global_config


def test_custom_provider_maps_credential_into_config(tmp_path: Path) -> None:
    catalog = default_catalog()
    generated = build_command(
        catalog,
        CommandPreset(
            "custom-tiingo",
            "自定义 Tiingo",
            "全球",
            "backtest",
            "strategies.example",
            data_source="tiingo",
        ),
        catalog.variable_values({"TIINGO_TOKEN": "tiingo-secret"}),
        tmp_path,
        strict=True,
    )
    config_value = generated.argv[generated.argv.index("--config") + 1]
    assert "'TIINGO_TOKEN': 'tiingo-secret'" in config_value


def test_all_catalog_presets_are_generation_complete(tmp_path: Path) -> None:
    catalog = default_catalog()
    variables = catalog.variable_values({})
    for item in catalog.variables:
        if not variables.get(item.name):
            variables[item.name] = "placeholder"

    for preset in catalog.presets:
        generated = build_command(catalog, preset, variables, tmp_path, strict=True)
        assert preset.strategy
        assert "--params" in generated.argv
        if preset.mode == "live":
            assert preset.connect
            assert preset.data_source
            assert preset.selection or generated.options.get("symbols")
        if preset.mode == "optimize":
            assert generated.options.get("opt_params")
        else:
            assert not generated.options.get("opt_params")
        if "--config" in generated.argv:
            ast.literal_eval(generated.argv[generated.argv.index("--config") + 1])


def test_build_command_uses_python_literal_and_resolves_environment(tmp_path: Path) -> None:
    catalog = default_catalog()
    variables = catalog.variable_values({"GM_TOKEN": "token-value", "GM_HOST": "127.0.0.1", "GM_PORT": "7001"})
    generated = build_command(catalog, catalog.preset("readme_gm_sim"), variables, tmp_path, strict=True)

    assert generated.argv[0] == sys.executable
    assert generated.argv[1:3] == ("-u", str(tmp_path / "run.py"))
    assert "--params" in generated.argv
    assert generated.argv[generated.argv.index("--params") + 1] == "{}"
    assert "start_date" not in generated.options
    config_value = generated.argv[generated.argv.index("--config") + 1]
    assert "token-value|127.0.0.1:7001" in config_value
    assert generated.warnings == ()

    custom = build_command(
        catalog,
        replace(catalog.preset("readme_gm_sim"), config={"KEEP_OVERNIGHT_ORDERS": True}),
        variables,
        tmp_path,
        strict=True,
    )
    assert "'KEEP_OVERNIGHT_ORDERS': True" in custom.argv[custom.argv.index("--config") + 1]

    futu = build_command(
        catalog,
        catalog.preset("readme_futu_sim"),
        catalog.variable_values({"FUTU_PORT": "12345", "CN_LOT_SIZE": "100"}),
        tmp_path,
    )
    assert "'FUTU_PORT': 12345" in futu.argv[futu.argv.index("--config") + 1]
    assert "'LOT_SIZE': 100" in futu.argv[futu.argv.index("--config") + 1]

    global_futu = build_command(
        catalog,
        CommandPreset(
            "global-futu", "global-futu", "示例", "backtest",
            "strategies.sample_auto_rebalance_strategy",
            data_source="futu", config_profile="futu_global",
        ),
        catalog.variable_values({"GLOBAL_LOT_SIZE": "0.00000001"}),
        tmp_path,
    )
    assert "'LOT_SIZE': 1e-08" in global_futu.argv[global_futu.argv.index("--config") + 1]


def test_build_command_normalizes_symbol_list_literal(tmp_path: Path) -> None:
    catalog = default_catalog()
    preset = CommandPreset(
        "symbol-list",
        "symbol list",
        "A股",
        "backtest",
        "strategies.example",
        options={"symbols": "['SHSE.600000', 'US.AAPL']"},
    )

    generated = build_command(catalog, preset, catalog.variable_values({}), tmp_path)

    symbols_index = generated.argv.index("--symbols")
    assert generated.argv[symbols_index + 1] == "SHSE.600000,US.AAPL"


def test_build_command_exposes_hybrid_provider_credentials(tmp_path: Path) -> None:
    catalog = default_catalog()
    preset = CommandPreset(
        "hybrid",
        "hybrid",
        "期权",
        "live",
        "strategies.example",
        data_source="theta+futu",
        connect="futu_broker:sim",
    )
    variables = catalog.variable_values({
        "THETADATA_TOKEN": "theta-token",
        "FUTU_HOST": "127.0.0.1",
        "FUTU_PORT": "11111",
    })

    generated = build_command(catalog, preset, variables, tmp_path, strict=True)

    config_value = generated.argv[generated.argv.index("--config") + 1]
    assert "THETADATA_TOKEN" in config_value
    assert "FUTU_HOST" in config_value


def test_build_command_maps_futu_account_fields_without_profile(tmp_path: Path) -> None:
    catalog = default_catalog()
    preset = CommandPreset(
        "custom-futu-account",
        "custom futu account",
        "期权",
        "backtest",
        "strategies.example",
        data_source="futu",
    )
    generated = build_command(
        catalog,
        preset,
        catalog.variable_values({
            "FUTU_HOST": "127.0.0.1",
            "FUTU_PORT": "11111",
            "FUTU_ACCOUNT_ID": "9",
            "FUTU_ACCOUNT_INDEX": "2",
            "FUTU_ACCOUNT_CURRENCY": "USD",
        }),
        tmp_path,
    )
    config_value = generated.argv[generated.argv.index("--config") + 1]
    assert "'FUTU_ACCOUNT_ID': 9" in config_value
    assert "'FUTU_ACCOUNT_INDEX': 2" in config_value
    assert "'FUTU_ACCOUNT_CURRENCY': 'USD'" in config_value


def test_missing_required_environment_is_reported() -> None:
    catalog = default_catalog()
    variables = catalog.variable_values({})
    generated = build_command(catalog, catalog.preset("readme_gm_sim"), variables, Path("."))
    assert "GM_TOKEN" in generated.warnings[0]
    with pytest.raises(MissingEnvironmentError):
        build_command(catalog, catalog.preset("readme_gm_sim"), variables, Path("."), strict=True)


def test_variable_values_reverse_display_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传入别名时验证反显；目录没有对应变量则跳过。"""

    catalog = _public_catalog(tmp_path, monkeypatch)
    _skip_unless_names(
        catalog,
        {"WECOM_WEBHOOK", "GM_TOKEN", "GM_HOST", "GM_PORT", "TIINGO_TOKEN", "FUTU_HOST"},
        "反显别名所需变量不存在",
    )
    values = catalog.variable_values(
        {
            "GLOBAL_WECOM_WEBHOOK": "https://example.invalid/wecom",
            "GM_TOKEN_SERVER": "srv|1.1.1.1:7001",
            "TIINGO_TOKEN": "tiingo-alias",
        }
    )
    assert values["WECOM_WEBHOOK"] == "https://example.invalid/wecom"
    assert values["GM_TOKEN"] == "srv"
    assert values["GM_HOST"] == "1.1.1.1"
    assert values["GM_PORT"] == "7001"
    assert values["TIINGO_TOKEN"] == "tiingo-alias"
    assert values["FUTU_HOST"] == ""

def test_variable_values_reverse_display_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """项目配置有值时验证反显；变量或配置缺失时跳过，不因本机私有层硬失败。"""

    catalog = _public_catalog(tmp_path, monkeypatch)
    _skip_unless_names(
        catalog,
        {
            "TIINGO_TOKEN",
            "SXSC_TUSHARE_TOKEN",
            "WECOM_WEBHOOK",
            "GM_TOKEN",
            "GM_HOST",
            "GM_PORT",
            "FUTU_HOST",
            "IBKR_HOST",
        },
        "项目配置反显所需变量不存在",
    )
    monkeypatch.setattr("command_center.catalog.os.environ", {}, raising=False)
    monkeypatch.setattr("command_center.catalog._command_set_candidate_paths", lambda: ())
    monkeypatch.setattr("config.TIINGO_TOKEN", "tiingo-from-config")
    monkeypatch.setattr("config.SXSC_TUSHARE_TOKEN", "sxsc-from-config")
    monkeypatch.setattr("config.WECOM_WEBHOOK", "https://example.invalid/config-wecom")
    monkeypatch.setattr("config.GM_TOKEN", "cfg|172.16.0.9:7001")
    values = catalog.variable_values()
    assert values["TIINGO_TOKEN"] == "tiingo-from-config"
    assert values["SXSC_TUSHARE_TOKEN"] == "sxsc-from-config"
    assert values["WECOM_WEBHOOK"] == "https://example.invalid/config-wecom"
    assert values["GM_TOKEN"] == "cfg"
    assert values["GM_HOST"] == "172.16.0.9"
    assert values["GM_PORT"] == "7001"
    import config as project_config

    for name in ("FUTU_HOST", "IBKR_HOST"):
        configured = str(getattr(project_config, name, "") or "").strip()
        if not configured:
            pytest.skip(f"项目配置没有 {name}")
        assert values[name] == configured

def test_variable_values_env_overrides_project_config(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = default_catalog()
    monkeypatch.setattr("command_center.catalog.os.environ", {"TIINGO_TOKEN": "from-env"}, raising=False)
    monkeypatch.setattr("config.TIINGO_TOKEN", "from-config")
    values = catalog.variable_values()
    assert values["TIINGO_TOKEN"] == "from-env"


def test_gm_token_parts_split_and_compose() -> None:
    from command_center.catalog import compose_gm_token, parse_gm_token

    assert parse_gm_token("tok|10.0.0.2:7001") == ("tok", "10.0.0.2", "7001")
    assert parse_gm_token("tok") == ("tok", "", "")
    assert compose_gm_token("tok", "10.0.0.2", "7001") == "tok|10.0.0.2:7001"
    assert compose_gm_token("tok|8.8.8.8:7002", "", "") == "tok|8.8.8.8:7002"


def test_gm_token_variable_values_split_legacy_and_composed() -> None:
    catalog = default_catalog()
    names = {item.name for item in catalog.variables}
    assert "GM_TOKEN" in names
    assert "GM_HOST" in names
    assert "GM_PORT" in names
    assert "GM_TOKEN_LOCAL" not in names
    assert "GM_TOKEN_SERVER" not in names

    split_local = catalog.variable_values({"GM_TOKEN_LOCAL": "tok|8.8.8.8:7002"})
    assert split_local["GM_TOKEN"] == "tok"
    assert split_local["GM_HOST"] == "8.8.8.8"
    assert split_local["GM_PORT"] == "7002"

    split_direct = catalog.variable_values({"GM_TOKEN": "abc|10.1.2.3:7009"})
    assert split_direct["GM_TOKEN"] == "abc"
    assert split_direct["GM_HOST"] == "10.1.2.3"
    assert split_direct["GM_PORT"] == "7009"


def test_gm_live_profile_uses_composed_serv_addr(tmp_path: Path) -> None:
    catalog = default_catalog()
    generated = build_command(
        catalog,
        CommandPreset(
            "gm-test", "gm-test", "示例", "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="gm", config_profile="gm_sim",
        ),
            catalog.variable_values({
                "GM_TOKEN": "tok", "GM_HOST": "10.1.2.3", "GM_PORT": "7009",
                "GM_STRATEGY_ID": "strategy-id",
            }),
        tmp_path,
        strict=True,
    )
    config = ast.literal_eval(generated.argv[generated.argv.index("--config") + 1])
    assert config["GM_TOKEN"] == "tok|10.1.2.3:7009"
    conn = config["BROKER_ENVIRONMENTS"]["gm_broker"]["sim"]
    assert conn["serv_addr"] == "10.1.2.3:7009"
    assert conn["token"] == "tok|10.1.2.3:7009"


def test_shell_rendering_and_raw_copy_preserve_values() -> None:
    argv = ["python", "run.py", "--config", "{'GM_TOKEN': 'abc$123'}"]
    powershell = render_shell_command(argv, "PowerShell")
    bash = render_shell_command(argv, "Bash")
    assert powershell.startswith("& ")
    assert "abc$123" in powershell
    assert "abc$123" in bash
    variables = render_variables({"GM_TOKEN": "abc$123"}, "PowerShell")
    assert "abc$123" in variables
    assert "$env:GM_TOKEN" in variables
    assert "\n" in render_display_command(argv, "Bash")
    wsl = render_shell_command(
        [r"E:\QuantAda\.venv\Scripts\python.exe", "-u", r"E:\QuantAda\run.py"],
        "Bash",
        "WSL",
    )
    assert "/mnt/e/QuantAda/run.py" in wsl
    powershell_wsl = render_shell_command(
        [r"E:\QuantAda\run.py"], "PowerShell", "WSL"
    )
    assert r"E:\QuantAda\run.py" in powershell_wsl


def test_portable_linux_command_replaces_local_interpreter_and_run_path() -> None:
    argv = [r"E:\QuantAda\.venv\Scripts\python.exe", "-u", r"E:\QuantAda\run.py", "strategies.demo"]
    assert portable_linux_argv(argv) == ("python", "run.py", "strategies.demo")
    assert render_portable_linux_display_command(argv) == "python run.py \\\n  strategies.demo"
    config_argv = [r"E:\QuantAda\.venv\Scripts\python.exe", "-u", r"E:\QuantAda\run.py", "--config", "{'LOG': False}"]
    rendered = render_portable_linux_display_command(config_argv)
    assert '"{\'LOG\': False}"' in rendered
    assert "'\"'" not in rendered


def test_platform_detection_has_interpreter() -> None:
    info = detect_platform()
    assert info["system"]
    assert info["python"]
    assert info["shell"]


def test_executor_runs_without_shell_and_collects_output(tmp_path: Path) -> None:
    executor = CommandExecutor(tmp_path)
    executor.start([sys.executable, "-c", "print('command-center-ok')"])
    deadline = time.monotonic() + 10
    events: list[tuple[str, str | int | float]] = []
    while time.monotonic() < deadline:
        events.extend(executor.drain())
        if not executor.running and any(kind == "done" for kind, _ in events):
            break
        time.sleep(0.02)
    assert any(kind == "line" and payload == "command-center-ok" for kind, payload in events)
    assert any(kind == "done" and payload == 0 for kind, payload in events)
    assert executor.process is not None and executor.process.stdout is not None
    assert executor.process.stdout.closed


def test_executor_does_not_start_new_run_while_previous_reader_is_finishing(tmp_path: Path) -> None:
    executor = CommandExecutor(tmp_path)
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def on_line(_line: str) -> None:
        callback_entered.set()
        release_callback.wait(timeout=5)

    executor.start([sys.executable, "-c", "print('first')"], on_line=on_line)
    assert callback_entered.wait(timeout=5)
    # 子进程可以已经退出，但 reader 仍被 UI 回调阻塞；此时不得复用
    # self.process 启动第二次运行。
    deadline = time.monotonic() + 5
    while executor.process is not None and executor.process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(RuntimeError, match="仍在收尾"):
        executor.start([sys.executable, "-c", "print('second')"])

    release_callback.set()
    deadline = time.monotonic() + 5
    while executor._reader is not None and executor._reader.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    executor.start([sys.executable, "-c", "print('second')"])
    deadline = time.monotonic() + 5
    while executor.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not executor.running


def test_gm_server_preset_keeps_profile_server_address(tmp_path: Path) -> None:
    catalog = default_catalog()
    variables = catalog.variable_values({"GM_TOKEN": "token"})
    generated = build_command(
        catalog,
        CommandPreset(
            "gm-server-test", "gm-server-test", "示例", "backtest",
            "strategies.sample_macd_cross_strategy",
            data_source="gm", config_profile="gm_sim",
        ),
        variables,
        tmp_path,
    )
    config_value = ast.literal_eval(generated.argv[generated.argv.index("--config") + 1])
    assert config_value["BROKER_ENVIRONMENTS"]["gm_broker"]["sim"]["serv_addr"] == "127.0.0.1:7001"




def test_catalog_data_sources_are_providers() -> None:
    catalog = default_catalog()
    known = {
        "gm",
        "futu",
        "tiingo",
        "akshare",
        "tushare",
        "sxsc_tushare",
        "csv",
        "theta",
        "thetadata",
        "ibkr",
    }
    profile_only = {item.profile_id for item in catalog.profiles} - known
    for preset in catalog.presets:
        if preset.data_source:
            assert preset.data_source not in profile_only
    assert catalog.preset("readme_futu_sim").data_source == "futu"


def test_executor_keeps_chinese_output(tmp_path: Path) -> None:
    executor = CommandExecutor(tmp_path)
    executor.start([sys.executable, "-c", "print('命令工作台')"])
    deadline = time.monotonic() + 10
    events: list[tuple[str, str | int | float]] = []
    while time.monotonic() < deadline:
        events.extend(executor.drain())
        if not executor.running and any(kind == "done" for kind, _ in events):
            break
        time.sleep(0.02)
    assert any(kind == "line" and payload == "命令工作台" for kind, payload in events)
    assert any(kind == "done" and payload == 0 for kind, payload in events)


def test_executor_decodes_gb18030_bytes(tmp_path: Path) -> None:
    executor = CommandExecutor(tmp_path)
    executor.start(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write('中文输出'.encode('gb18030') + b'\\n')"]
    )
    deadline = time.monotonic() + 10
    events: list[tuple[str, str | int | float]] = []
    while time.monotonic() < deadline:
        events.extend(executor.drain())
        if not executor.running and any(kind == "done" for kind, _ in events):
            break
        time.sleep(0.02)
    assert any(kind == "line" and payload == "中文输出" for kind, payload in events)


def test_executor_can_stop_current_process(tmp_path: Path) -> None:
    executor = CommandExecutor(tmp_path)
    executor.start([sys.executable, "-c", "import time; time.sleep(30)"])
    assert executor.running
    assert executor.stop()
    deadline = time.monotonic() + 10
    while executor.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not executor.running

def test_variable_values_fill_placeholders_from_command_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令集只回填空值；已有项目配置时验证保留，没有则验证命令集回填。"""

    path = tmp_path / "cmd.ps1"
    path.write_text(
        "export TIINGO_TOKEN='from-ps1'\n"
        "export GM_TOKEN_LOCAL='tok|8.8.8.8:7002'\n"
        "export FUTU_HOST='9.9.9.9'\n"
        "export SKIP_VALUE='$OTHER/path'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "command_center.catalog._command_set_candidate_paths",
        lambda: (path,),
    )
    monkeypatch.setattr("command_center.catalog.os.environ", {}, raising=False)
    catalog = _public_catalog(tmp_path, monkeypatch)
    _skip_unless_names(
        catalog,
        {"TIINGO_TOKEN", "GM_TOKEN", "GM_HOST", "GM_PORT", "FUTU_HOST"},
        "命令集回填所需变量不存在",
    )
    values = catalog.variable_values()
    assert values["TIINGO_TOKEN"] == "from-ps1"
    assert values["GM_TOKEN"] == "tok"
    assert values["GM_HOST"] == "8.8.8.8"
    assert values["GM_PORT"] == "7002"
    import config as project_config

    configured = str(getattr(project_config, "FUTU_HOST", "") or "").strip()
    if configured:
        assert values["FUTU_HOST"] == configured
    else:
        assert values["FUTU_HOST"] == "9.9.9.9"

def test_parse_command_set_exports_skips_unexpanded_variables() -> None:
    from command_center.catalog import _parse_command_set_exports

    parsed = _parse_command_set_exports(
        "export TIINGO_TOKEN='abc'\nexport BAD='$GM_TOKEN_LOCAL'\nexport HOMEISH='$HOME/.futursa'\n"
    )
    assert parsed["TIINGO_TOKEN"] == "abc"
    assert "BAD" not in parsed
    assert parsed["HOMEISH"].endswith(".futursa")

