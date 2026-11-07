"""运行时解析配置值的回归测试。"""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_config_facade_exports_public_subconfig_groups():
    import config

    assert config.LOT_SIZE == 1
    assert config.TIINGO_TOKEN == "your_token_here"
    assert config.THETADATA_TOKEN == "your_token_here"
    assert hasattr(config, "SEC_USER_AGENT")
    assert config.DATA_PROVIDER_ALIASES["theta_futu_sec"] == "thetadata+futu,sec"
    assert config.DATA_PROVIDER_ALIASES["eodhd_theta_sec"] == "eodhd,theta,sec"
    assert config.DATA_PROVIDER_ALIASES["theta_futu_research"] == "thetadata+futu,sec,earnings,rates"
    assert config.DATA_PROVIDER_ALIASES["us_options_research"] == "thetadata+futu,sec,cboe"
    assert config.ALARM_LEVEL == "INFO"
    assert config.IBKR_PORT == 7497
    assert config.GM_BROKER_ENVIRONMENTS["gm_broker"]["sim"]["schedule"] == "1d:14:45:00"
    assert config.IB_BROKER_ENVIRONMENTS["ib_broker"]["real"]["timezone"] == "America/New_York"
    assert config.FUTU_HOST == os.getenv('FUTU_HOST', '127.0.0.1')
    assert config.FUTU_PORT == int(os.getenv('FUTU_PORT', '11111'))
    assert config.FUTU_RSA_KEY_PATH == os.getenv('FUTU_RSA_KEY_PATH', '')
    assert config.FUTU_TRADE_PASSWORD_ENV == ''
    assert config.FUTU_TRADE_PASSWORD_MD5_ENV == ''
    assert config.FUTU_TRADE_PASSWORD == ''
    assert config.FUTU_TRADE_PASSWORD_MD5 == ''
    assert config.FUTU_ACCOUNT_ID == 0
    assert config.FUTU_ACCOUNT_INDEX == 0
    assert config.FUTU_ACCOUNT_CURRENCY == 'HKD'
    assert config.FUTU_TRADE_ENV == 'SIMULATE'
    assert config.FUTU_ACCOUNT_ID == 0
    assert not hasattr(config, 'THETADATA_API_KEY')
    assert config.FUTU_BROKER_ENVIRONMENTS['futu_broker']['sim']['trd_env'] == 'SIMULATE'
    assert config.BROKER_ENVIRONMENTS["ib_broker"]["real"]["timezone"] == "America/New_York"
    assert config.has_alarm_webhook() == bool(
        os.getenv('DINGTALK_WEBHOOK') or os.getenv('WECOM_WEBHOOK')
    )
    expected_alarms_enabled = (
        bool(config.ALARMS_ENABLED)
        if config.ALARMS_ENABLED is not None
        else config.has_alarm_webhook()
    )
    assert config.is_alarms_enabled() == expected_alarms_enabled


def test_config_import_maps_same_named_environment_values_to_flattened_keys():
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "FUTU_HOST": "192.0.2.10",
            "FUTU_PORT": "22222",
            "FUTU_RSA_KEY_PATH": "/tmp/futu-key",
            "CACHE_DATA": "true",
            "LOT_SIZE": "7",
            "DATA_PATH": "/tmp/env-data",
            "ALARMS_ENABLED": "false",
            "FUTU_BROKER_ENVIRONMENTS": (
                "{'futu_broker': {'sim': {'schedule': '1d:12:00:00'}}}"
            ),
        }
    )
    env.pop("QUANTADA_TEST_MODE", None)
    code = (
        "import config; config.apply_environment_overrides(); import json; "
        "print(json.dumps({'host': config.FUTU_HOST, 'port': config.FUTU_PORT, "
        "'rsa': config.FUTU_RSA_KEY_PATH, 'cache': config.CACHE_DATA, "
        "'alarms': config.ALARMS_ENABLED, "
        "'lot': config.LOT_SIZE, 'path': config.DATA_PATH, "
        "'schedule': config.BROKER_ENVIRONMENTS['futu_broker']['sim']['schedule']}, "
        "sort_keys=True))"
    )
    output = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        text=True,
    )
    values = json.loads(output.strip().splitlines()[-1])

    assert values == {
        "cache": True,
        "alarms": False,
        "host": "192.0.2.10",
        "lot": 7,
        "path": "/tmp/env-data",
        "port": 22222,
        "rsa": "/tmp/futu-key",
        "schedule": "1d:12:00:00",
    }


def test_config_import_keeps_default_when_environment_type_is_invalid():
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["FUTU_PORT"] = "invalid-port"
    env.pop("QUANTADA_TEST_MODE", None)
    code = "import config; config.apply_environment_overrides(); print(config.FUTU_PORT)"
    output = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    )

    assert output.strip().splitlines()[-1] == "11111"


def test_config_test_mode_skips_environment_mapping():
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({"QUANTADA_TEST_MODE": "1", "FUTU_PORT": "22222"})
    code = "import config; config.apply_environment_overrides(); print(config.FUTU_PORT)"
    output = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=project_root,
        env=env,
        text=True,
    )

    assert output.strip() == "11111"


def test_run_main_overrides_futu_public_keys_like_other_config(monkeypatch, capsys):
    import sys
    import config
    import run

    monkeypatch.setattr(
        sys,
        'argv',
        [
            'run.py',
            'dummy_strategy',
            '--start_date',
            '20230101',
            '--config',
            "{'FUTU_HOST': '192.0.2.10', 'FUTU_PORT': 22222, 'FUTU_RSA_KEY_PATH': 'key.pem', 'SEC_USER_AGENT': 'QuantAda test test@example.com'}",
        ],
    )
    monkeypatch.setattr(config, 'DB_ENABLED', False, raising=False)
    monkeypatch.setattr(config, 'HTTP_LOG_URL', None, raising=False)
    for name in ('FUTU_HOST', 'FUTU_PORT', 'FUTU_RSA_KEY_PATH', 'SEC_USER_AGENT'):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(run, 'run_backtest', lambda **kwargs: None)

    run._run_main()

    assert config.FUTU_HOST == '192.0.2.10'
    assert config.FUTU_PORT == 22222
    assert config.FUTU_RSA_KEY_PATH == 'key.pem'
    assert config.SEC_USER_AGENT == 'QuantAda test test@example.com'
    output = capsys.readouterr().out
    assert "[Config] Overriding FUTU_HOST = 192.0.2.10" in output
    assert "[Config] Overriding FUTU_PORT = 22222" in output
    assert "[Config] Overriding FUTU_RSA_KEY_PATH = key.pem" in output
    assert "[Config] Overriding SEC_USER_AGENT = QuantAda test test@example.com" in output


def test_run_main_prints_private_futu_unlock_value_in_internal_mode(monkeypatch, capsys):
    import sys
    import config
    import run

    monkeypatch.setattr(
        sys,
        'argv',
        [
            'run.py',
            'dummy_strategy',
            '--start_date',
            '20230101',
            '--config',
            "{'FUTU_TRADE_PASSWORD': 'fixture'}",
        ],
    )
    monkeypatch.setattr(config, 'DB_ENABLED', False, raising=False)
    monkeypatch.setattr(config, 'HTTP_LOG_URL', None, raising=False)
    monkeypatch.setattr(config, 'FUTU_TRADE_PASSWORD', '', raising=False)
    monkeypatch.setattr(run, 'run_backtest', lambda **kwargs: None)

    run._run_main()

    output = capsys.readouterr().out
    assert 'fixture' in output


def test_run_main_allows_explicitly_imported_broker_environment_dicts(monkeypatch, capsys):
    import sys
    import config
    import run
    import configs.manager as config_manager

    original = config.FUTU_BROKER_ENVIRONMENTS
    original_aggregate = config.BROKER_ENVIRONMENTS
    original_manager_aggregate = config_manager.BROKER_ENVIRONMENTS
    monkeypatch.setattr(config, 'BROKER_ENVIRONMENTS', original_aggregate)
    monkeypatch.setattr(config_manager, 'BROKER_ENVIRONMENTS', original_manager_aggregate)
    override = {'futu_broker': {'real': {'schedule': '1d:15:00:00'}}}
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'run.py',
            'dummy_strategy',
            '--start_date',
            '20230101',
            '--config',
            "{'FUTU_BROKER_ENVIRONMENTS': {'futu_broker': {'real': {'schedule': '1d:15:00:00'}}}}",
        ],
    )
    monkeypatch.setattr(config, 'DB_ENABLED', False, raising=False)
    monkeypatch.setattr(config, 'HTTP_LOG_URL', None, raising=False)
    monkeypatch.setattr(run, 'run_backtest', lambda **kwargs: None)

    run._run_main()

    assert config.FUTU_BROKER_ENVIRONMENTS == override
    assert config.FUTU_BROKER_ENVIRONMENTS is not original
    assert config.BROKER_ENVIRONMENTS['futu_broker'] == override['futu_broker']
    assert "[Config] Overriding FUTU_BROKER_ENVIRONMENTS" in capsys.readouterr().out


def test_csv_provider_resolves_data_path_when_constructed(monkeypatch, tmp_path):
    import data_providers.csv_provider as csv_module

    configured_path = tmp_path / "runtime-data"
    monkeypatch.setattr(csv_module.config, "DATA_PATH", str(configured_path))

    provider = csv_module.CsvDataProvider()

    assert provider.data_path == str(configured_path)
    assert configured_path.is_dir()
    assert (configured_path / "market_cache").is_dir()


def test_tushare_provider_resolves_token_when_constructed(monkeypatch):
    import data_providers.tushare_provider as tushare_module

    calls = []
    sentinel = object()
    monkeypatch.setattr(tushare_module.config, "TUSHARE_TOKEN", "runtime-token")
    monkeypatch.setattr(tushare_module.ts, "set_token", calls.append)
    monkeypatch.setattr(tushare_module.ts, "pro_api", lambda: sentinel)

    provider = tushare_module.TushareDataProvider()

    assert calls == ["runtime-token"]
    assert provider.pro is sentinel


def test_sxsc_tushare_provider_resolves_token_when_constructed(monkeypatch):
    import data_providers.sxsctushare_provider as sxsc_module

    monkeypatch.setattr(sxsc_module.config, "SXSC_TUSHARE_TOKEN", "runtime-token")

    provider = sxsc_module.SxscTushareDataProvider()

    assert provider.token == "runtime-token"


def test_tiingo_provider_resolves_token_when_constructed(monkeypatch):
    import data_providers.tiingo_provider as tiingo_module

    captured = {}

    class DummyClient:
        def __init__(self, options):
            captured["options"] = options

    monkeypatch.setattr(tiingo_module.config, "TIINGO_TOKEN", "runtime-token")
    monkeypatch.setattr(tiingo_module, "TiingoClient", lambda options: DummyClient(options))

    provider = tiingo_module.TiingoDataProvider()

    assert provider.client is not None
    assert captured["options"]["api_key"] == "runtime-token"


def test_csv_provider_creates_missing_parent_and_cache_subdir(tmp_path):
    import data_providers.csv_provider as csv_module

    configured_path = tmp_path / "missing-parent" / "runtime-data"
    assert not configured_path.exists()
    provider = csv_module.CsvDataProvider(data_path=str(configured_path))
    assert provider.data_path == str(configured_path)
    assert (configured_path / "market_cache").is_dir()
