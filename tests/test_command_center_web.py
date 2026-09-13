"""命令工作台 HTTP 服务的核心行为测试。"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from command_center.web import CommandCenterService, serve
from command_center.web_static import STATIC_FILE, get_index_html
from common.terminal_log import OPTIMIZER_AI_ANALYSIS_START_MARKER


def test_web_html_is_external_static_resource():
    assert STATIC_FILE.name == "index.html"
    assert STATIC_FILE.is_file()
    html = get_index_html()
    assert "QuantAda Command Center" in html
    assert 'id="strategy-input"' in html
    assert 'id="strategy-toggle"' in html
    assert 'id="strategy-options"' in html
    assert 'list="strategy-list"' not in html
    assert 'id="strategy-picker"' not in html
    assert 'id="risk-params-input"' in html
    assert 'id="view-training-log"' not in html
    assert 'data-log-result=' in html
    assert 'id="training-log-modal"' in html
    assert 'class="training-group-toggle"' in html
    assert 'class="training-chevron"' in html
    assert "function isTrainingGroupOpen(" in html
    assert "trainingResultGroupLabel(" in html
    assert "data-group-index=" in html
    assert "aria-expanded=" in html
    assert '/api/training/log' in html
    assert 'id="language-select"' in html
    assert "quantada.ui.language" in html
    assert 'class="topbar-control platform-control"' in html
    assert 'class="topbar-control language-switch"' in html
    assert html.index('id="reload-btn"') < html.index('id="language-select"')
    assert 'class="brand-mark"' not in html
    assert 'flex:0 0 8px' in html
    assert '.topbar{flex-wrap:wrap' in html
    assert '.brand{order:1;flex:1 1 auto' in html
    assert "state.source_root=selected.source_root||''" in html
    assert "refreshCommand();}});" in html
    assert '.language-switch{order:2;flex:0 0 auto' in html
    assert '.platform-control select{width:150px;min-width:120px}' in html
    assert 'id="print-plan"' in html
    assert "option_margin_stress_down:'option-margin-stress-down-input'" in html
    assert "option_volatility_shock:'option-volatility-shock-input'" in html
    assert "options.option_portfolio_margin" in html
    assert 'value="">新建配置</option>' in html
    assert '方案别名</label>' in html
    assert 'id="config-profile-select"' in html
    assert "function renderConfigProfiles(" in html
    assert "replace(/[+,]/g,' ')" in html
    assert '新建配置' in html
    assert '打印交易计划（PRINT_PLAN）' not in html
    assert "GLOBAL_WECOM_WEBHOOK" not in html
    assert "'市场与数量','通知'" in html
    assert 'data-tab="variables"' not in html
    assert 'id="tab-variables"' not in html
    assert 'id="variables-editor"' in html
    assert "strategy:''" in html
    assert "/api/strategy/params" in html
    assert "clearOptionInputs(); Object.entries(state.form.options||{})" in html
    assert "function syncTrainingRangesFromOptions(" in html
    assert "syncTrainingRangesFromOptions(state.form.options)" in html
    assert "function optionInput(" in html
    assert "replace(/_/g,'-')" in html
    assert "function syncConfigProfile(" in html
    assert "state.form.config_profile='none'" in html
    assert "type==='string'" in html
    assert "el.type==='date'" in html
    assert 'id="copy-result"' in html
    assert 'id="copy-all"' not in html
    assert "setSelectValue(" in html
    assert "selectFormKey(" in html
    assert "setFollowText(" in html
    assert "attachFollowTail(" in html
    assert "运行在当前 QuantAda 项目目录" not in html
    assert "页面只通过本地 API 通信" not in html
    assert "可审计的策略实验与运行工作台" not in html
    assert "不绘图（--no_plot）" not in html
    assert "刷新数据（--refresh）" not in html
    assert 'id="no-plot"> 不绘图</label>' in html
    assert 'id="refresh"> 刷新数据</label>' in html
    assert 'class="footer"' not in html
    assert "配置完成后自动生成命令" not in html
    assert "方案别名会自动作为默认说明" not in html
    assert "选择策略源码并分析后显示建议范围" not in html
    assert 'id="source-path" placeholder="例如 strategies/xxx.py"' in html
    assert "GM_TOKEN_LOCAL" not in html
    assert "GM 本地 token" not in html
    assert "GM 服务器 token" not in html
    assert "function parseGmToken(" in html
    assert "'GM 服务地址':'GM host'" in html
    assert "payload.mode!=='live'" in html
    assert "delete payload.options.opt_params" in html
    assert "newConfiguration(false)" in html
    assert "await api('/api/profiles')" in html
    assert "'profile-name'" in html
    assert "bindProfileDefaults" not in html
    assert "gm_broker:sim" in html
    assert "gm_sim" not in html
    assert "market:'自定义'" in html
    assert "p.gm_host" in html
    assert "setFollowText(content," in html


def test_web_gm_token_fields_compose_into_config(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate(
        {
            "strategy": "strategies.example",
            "data_source": "gm",
            "config_profile": "gm_local",
            "variables": {"GM_TOKEN": "abc", "GM_HOST": "10.0.0.2", "GM_PORT": "7001"},
        }
    )
    assert "'GM_TOKEN': 'abc|10.0.0.2:7001'" in result["display_command"]


def test_web_service_generates_custom_command(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate(
        {
            "strategy": "strategies.example",
            "selection": "stock_selectors.example",
            "data_source": "gm",
            "params": {"period": 20},
            "options": {"no_plot": True},
            "variables": {"PYTHON_EXECUTABLE": "python"},
        }
    )
    assert result["argv"][0] == "python"
    assert "--no_plot" in result["argv"]
    assert "period" in result["params"]
    assert result["display_command"].startswith("python run.py")


def test_custom_tiingo_command_has_explicit_symbol_options_only(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate({"strategy": "strategies.example", "data_source": "tiingo"})
    assert "symbols" not in result["options"]
    assert "SHSE.600519" not in result["display_command"]


def test_web_profile_list_separates_public_readme_and_private_command_set(tmp_path):
    service = CommandCenterService(tmp_path)
    service.save_profile({"name": "我的方案", "strategy": "strategies.example"})
    profiles = service.profile_list()
    origins = [item["origin"] for item in profiles]
    assert origins[0] == "README"
    assert "私有命令集" not in origins


def test_web_service_loads_saved_profile_by_id(tmp_path):
    service = CommandCenterService(tmp_path)
    profile = service.save_profile({"name": "demo", "strategy": "strategies.example"})
    result = service.generate({"profile_id": profile["profile_id"]})
    assert result["argv"][3] == "strategies.example"
    assert result["options"]["desc"] == "demo"


def test_web_service_loads_and_edits_private_preset_params(tmp_path):
    private_dir = tmp_path / ".data" / "command_center"
    private_dir.mkdir(parents=True)
    (private_dir / "private_catalog.json").write_text(
        json.dumps(
            {
                "presets": [
                    {
                        "id": "private-opt",
                        "title": "私有优化",
                        "market": "全球",
                        "mode": "optimize",
                        "strategy": "strategies.example",
                        "params": {"trend_score_period": 80},
                        "options": {
                            "symbols": "US.AAPL",
                            "opt_params": {
                                "trend_score_period": {
                                    "type": "int",
                                    "low": 20,
                                    "high": 150,
                                    "step": 5,
                                }
                            },
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = CommandCenterService(tmp_path)

    listed = next(item for item in service.profile_list() if item["profile_id"] == "private-opt")
    assert listed["params"] == {"trend_score_period": 80}
    assert listed["options"]["opt_params"]["trend_score_period"]["high"] == 150

    loaded = service.generate({"profile_id": "private-opt"})
    assert loaded["params"] == {"trend_score_period": 80}
    assert loaded["options"]["opt_params"]["trend_score_period"]["high"] == 150

    edited = service.generate(
        {
            "profile_id": "private-opt",
            "params": {"trend_score_period": 90},
            "options": {
                "symbols": "US.MSFT",
                "opt_params": {
                    "trend_score_period": {
                        "type": "int",
                        "low": 30,
                        "high": 120,
                        "step": 10,
                    }
                },
            },
        }
    )
    assert edited["params"] == {"trend_score_period": 90}
    assert edited["options"]["symbols"] == "US.MSFT"
    assert edited["options"]["opt_params"]["trend_score_period"]["high"] == 120


def test_web_service_applies_form_edits_over_loaded_profile(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate(
        {
            "profile_id": "readme_macd_optimize",
            "strategy": "strategies.example",
            "mode": "optimize",
            "data_source": "theta",
            "params": {"period": 30},
            "options": {
                "opt_params": {
                    "period": {"type": "int", "low": 5, "high": 10, "step": 1}
                }
            },
        }
    )

    assert result["argv"][3] == "strategies.example"
    assert result["options"]["opt_params"]["period"]["high"] == 10
    assert result["params"] == {"period": 30}
    assert "--opt_params" in result["argv"]


def test_web_service_saves_form_config_override_and_options(tmp_path):
    service = CommandCenterService(tmp_path)
    profile = service.save_profile(
        {
            "name": "demo-form",
            "form": {
                "strategy": "strategies.example",
                "params": {"period": 30},
                "options": {"no_plot": True},
                "config_override": {"LOG": False},
                "market": "A股",
                "mode": "backtest",
            },
        }
    )
    assert profile["params"] == {"period": 30}
    assert profile["options"]["desc"] == "demo-form"
    assert profile["config_override"] == {"LOG": False}


def test_web_service_updates_user_profile_in_place(tmp_path):
    service = CommandCenterService(tmp_path)
    created = service.save_profile({"name": "demo", "strategy": "strategies.example", "params": {"period": 10}})
    updated = service.save_profile(
        {
            "name": "demo",
            "save_profile_id": created["profile_id"],
            "strategy": "strategies.example",
            "params": {"period": 20},
        }
    )
    assert updated["profile_id"] == created["profile_id"]
    assert updated["params"] == {"period": 20}
    assert len(service.profile_store.list()) == 1


def test_web_service_rejects_in_place_update_of_builtin_profile(tmp_path):
    service = CommandCenterService(tmp_path)
    with pytest.raises(ValueError, match="内置方案只读"):
        service.save_profile(
            {
                "name": "GM 模拟连接示例",
                "save_profile_id": "readme_gm_sim",
                "strategy": "strategies.example",
            }
        )


def test_web_service_accepts_config_override_for_custom_generation(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate(
        {
            "strategy": "strategies.example",
            "config_override": {"LOG": False},
        }
    )
    assert result["config"]["LOG"] is False
    assert "'LOG': False" in result["argv"][result["argv"].index("--config") + 1]


def test_web_custom_notification_variable_is_forwarded_to_config(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate(
        {
            "strategy": "strategies.example",
            "variables": {"DINGTALK_WEBHOOK": "https://example.invalid/ding"},
        }
    )
    assert result["config"]["DINGTALK_WEBHOOK"] == "https://example.invalid/ding"


def test_web_service_rejects_mode_that_does_not_match_run_dispatch(tmp_path):
    service = CommandCenterService(tmp_path)
    with pytest.raises(ValueError, match="实盘模式"):
        service.generate({"strategy": "strategies.example", "mode": "live"})
    with pytest.raises(ValueError, match="优化模式"):
        service.generate({"strategy": "strategies.example", "mode": "optimize"})
    with pytest.raises(ValueError, match="优化参数"):
        service.generate(
            {
                "strategy": "strategies.example",
                "mode": "backtest",
                "options": {"opt_params": {"period": {"type": "int", "low": 1, "high": 2, "step": 1}}},
            }
        )
    with pytest.raises(ValueError, match="运行模式设为实盘"):
        service.generate(
            {
                "strategy": "strategies.example",
                "mode": "backtest",
                "connect": "gm_broker:sim",
            }
        )
    with pytest.raises(ValueError, match="运行模式必须是"):
        service.generate({"strategy": "strategies.example", "mode": "unknown"})


def test_web_live_generation_requires_credentials_even_without_strict_checkbox(tmp_path):
    service = CommandCenterService(tmp_path)
    with pytest.raises(ValueError, match="缺少环境变量"):
        service.generate(
            {
                "strategy": "strategies.example",
                "mode": "live",
                "connect": "gm_broker:sim",
                "data_source": "gm",
                "options": {"symbols": "SHSE.600000"},
                "variables": {"GM_TOKEN": "", "GM_HOST": "", "GM_PORT": ""},
            }
        )


def test_web_service_analyzes_strategy_source(tmp_path):
    source = tmp_path / "strategies" / "demo.py"
    source.parent.mkdir()
    source.write_text("class Demo:\n    params = {'period': 20, 'enabled': True}\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    result = service.analyze_training({"source_path": "strategies/demo.py"})
    assert result["strategy"] == "strategies.demo"
    assert result["opt_params"]["period"]["type"] == "int"


def test_web_service_reads_strategy_params_by_module_path(tmp_path):
    source = tmp_path / "strategies" / "demo.py"
    source.parent.mkdir()
    source.write_text("class Demo:\n    params = {'period': 20, 'enabled': True}\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    result = service.strategy_params({"strategy": "strategies.demo"})
    assert Path(result["source_path"]) == source.resolve()
    assert result["params"] == {"period": 20, "enabled": True}


def test_web_service_reads_selected_training_log(tmp_path):
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    log = optimizer / "optimizer_terminal_demo.log"
    log.write_text(f"Best Training Score (sharpe): 1.2\nold training output\n{OPTIMIZER_AI_ANALYSIS_START_MARKER}\nAI analysis only\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    result_id = service.training_results()[0]["result_id"]
    payload = service.training_log({"result_id": result_id})
    assert payload["content"] == "AI analysis only"
    assert payload["marker_found"] is True
    assert payload["truncated"] is False


def test_web_service_hides_training_log_before_analysis_marker(tmp_path):
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    log = optimizer / "optimizer_terminal_demo.log"
    log.write_text("Best Training Score (sharpe): 1.2\nParams: {'period': 20}\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    result_id = service.training_results()[0]["result_id"]
    payload = service.training_log({"result_id": result_id})
    assert payload["content"] == ""
    assert payload["marker_found"] is False


def test_web_service_returns_empty_params_for_unknown_strategy(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.strategy_params({"strategy": "strategies.missing"})
    assert result["params"] == {}
    assert result["source_path"] is None


def test_web_api_state_and_generate(tmp_path):
    server = serve(tmp_path, port=0, open_browser=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert "variables" in state
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "QuantAda Command Center" in page
        catalog = json.loads(urllib.request.urlopen(base + "/api/catalog").read())
        assert "data_sources" in catalog["catalog"]
        profiles = json.loads(urllib.request.urlopen(base + "/api/profiles").read())
        assert any(item.get("builtin") for item in profiles["profiles"])
        request = urllib.request.Request(
            base + "/api/command",
            data=json.dumps({"strategy": "strategies.example"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        generated = json.loads(urllib.request.urlopen(request).read())
        assert generated["argv"][2].endswith("run.py")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_web_service_executes_and_polls_output(tmp_path):
    (tmp_path / "run.py").write_text("print('web-ok')\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    started = service.execute(
        {
            "strategy": "strategies.example",
            "variables": {"PYTHON_EXECUTABLE": sys.executable},
        }
    )
    run_id = started["run_id"]
    deadline = time.monotonic() + 10
    snapshot = service.run_snapshot(run_id)
    output = list(snapshot["output"])
    while snapshot["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
        snapshot = service.run_snapshot(run_id, snapshot["next_offset"])
        output.extend(snapshot["output"])
    assert not snapshot["running"]
    assert snapshot["return_code"] == 0
    assert "web-ok" in "\n".join(output)


def test_web_service_stop_only_targets_active_run(tmp_path):
    (tmp_path / "run.py").write_text("print('done')\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    first = service.execute(
        {"strategy": "strategies.example", "variables": {"PYTHON_EXECUTABLE": sys.executable}}
    )
    deadline = time.monotonic() + 10
    while service.run_snapshot(first["run_id"])["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not service.stop(first["run_id"])

    (tmp_path / "run.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
    second = service.execute(
        {"strategy": "strategies.example", "variables": {"PYTHON_EXECUTABLE": sys.executable}}
    )
    assert service.stop(first["run_id"]) is False
    assert service.stop(second["run_id"]) is True
    deadline = time.monotonic() + 10
    while service.executor.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not service.executor.running


def test_public_futu_generate_uses_futu_source(tmp_path):
    service = CommandCenterService(tmp_path)
    result = service.generate({"preset_id": "readme_futu_sim"})
    assert result["argv"][result["argv"].index("--data_source") + 1] == "futu"
    catalog = service.catalog_snapshot()
    assert "futu" in catalog["data_sources"]
    assert "futu_global" not in catalog["data_sources"]


def test_hybrid_catalog_profile_resolves_theta_and_futu_configuration(tmp_path):
    service = CommandCenterService(tmp_path)
    catalog = service.catalog_snapshot()
    profile = next(item for item in catalog["config_profiles"] if item["id"] == "theta_futu_global")
    assert "ThetaData" in profile["title"]
    result = service.generate(
        {
            "strategy": "strategies.example",
            "data_source": "theta+futu",
            "config_profile": "theta_futu_global",
            "variables": {
                "THETADATA_TOKEN": "theta-token",
                "FUTU_HOST": "127.0.0.1",
                "FUTU_PORT": "11111",
                "FUTU_RSA_KEY_PATH": "key.pem",
                "FUTU_TRADE_PASSWORD_ENV": "TEST_UNLOCK_SLOT",
            },
        }
    )
    assert result["argv"][result["argv"].index("--data_source") + 1] == "theta+futu"
    assert result["config"]["THETADATA_TOKEN"] == "theta-token"
    assert result["config"]["FUTU_TRADE_PASSWORD_ENV"] == "TEST_UNLOCK_SLOT"
    assert "fixture" not in repr(result)


def test_command_center_loads_private_catalog_from_source_root(tmp_path):
    source_root = tmp_path / "private-strategies"
    (source_root / ".data").mkdir(parents=True)
    (source_root / ".data" / "private_catalog.json").write_text(
        '{"presets": [{"id": "private-source", "strategy": "private.alpha"}]}',
        encoding="utf-8",
    )

    service = CommandCenterService(tmp_path / "public-project", source_root=source_root)

    assert service.catalog.preset("private-source").strategy == "private.alpha"


def test_private_trade_password_is_used_for_execution_and_visible_in_preview(tmp_path):
    service = CommandCenterService(tmp_path)
    saved = service.save_profile(
        {
            "name": "Futu 私有实盘",
            "strategy": "strategies.example",
            "data_source": "futu",
            "mode": "live",
            "connect": "futu_broker:real",
            "config_profile": "futu_global",
            "variables": {"FUTU_TRADE_PASSWORD": "fixture"},
        }
    )
    generated = service.generate({"profile_id": saved["profile_id"]})
    assert "fixture" in repr(generated)
    assert generated["config"]["FUTU_TRADE_PASSWORD"] == "fixture"


def test_saved_user_profile_roundtrip_keeps_futu_config(tmp_path):
    service = CommandCenterService(tmp_path)
    saved = service.save_profile(
        {
            "name": "自定义全球 Futu",
            "strategy": "strategies.example",
            "data_source": "futu",
            "market": "全球",
            "mode": "backtest",
            "config_profile": "futu_global",
            "options": {"no_plot": True, "train_roll_period": "5y"},
        }
    )
    loaded = next(item for item in service.profile_list() if item["profile_id"] == saved["profile_id"])
    assert loaded["data_source"] == "futu"
    assert loaded["config_profile"] == "futu_global"
    assert loaded["options"]["train_roll_period"] == "5y"
    generated = service.generate({"preset_id": saved["profile_id"]})
    assert generated["argv"][generated["argv"].index("--data_source") + 1] == "futu"
    assert generated["options"]["train_roll_period"] == "5y"


def test_saved_user_profile_roundtrip_keeps_hybrid_config_profile(tmp_path):
    service = CommandCenterService(tmp_path)
    saved = service.save_profile(
        {
            "name": "Theta Futu 混合方案",
            "strategy": "strategies.example",
            "data_source": "theta+futu",
            "market": "全球",
            "mode": "backtest",
            "config_profile": "theta_futu_global",
            "variables": {
                "FUTU_TRADE_PASSWORD_ENV": "TEST_UNLOCK_SLOT",
            },
        }
    )
    loaded = next(item for item in service.profile_list() if item["profile_id"] == saved["profile_id"])
    assert loaded["data_source"] == "theta+futu"
    assert loaded["config_profile"] == "theta_futu_global"
    assert "variables" not in loaded
    generated = service.generate({"preset_id": saved["profile_id"]})
    assert generated["argv"][generated["argv"].index("--data_source") + 1] == "theta+futu"


def test_custom_form_without_config_profile_does_not_inherit_builtin_profile(tmp_path):
    service = CommandCenterService(tmp_path)
    generated = service.generate(
        {
            "strategy": "strategies.example",
            "data_source": "futu",
            "mode": "backtest",
            "config_profile": "none",
        }
    )
    assert generated["config"].get("LOT_SIZE") in (None, "")

def test_gm_profile_list_exposes_serv_addr(tmp_path):
    service = CommandCenterService(tmp_path)
    profiles = {item["profile_id"]: item for item in service.profile_list()}
    assert "cn_gm_server" not in profiles
    assert "cn_gm_sim" not in profiles

