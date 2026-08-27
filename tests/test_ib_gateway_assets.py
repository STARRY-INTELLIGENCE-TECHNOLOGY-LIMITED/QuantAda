import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ROOT = ROOT / "ib_gateway"
WINDOWS_ROOT = GATEWAY_ROOT / "ibc_windows"
DOCKER_ROOT = GATEWAY_ROOT / "ib_docker"


def _read_ini(path: Path):
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_gateway_assets_use_new_cohesive_directory():
    assert GATEWAY_ROOT.is_dir()
    assert WINDOWS_ROOT.is_dir()
    assert DOCKER_ROOT.is_dir()
    assert not (ROOT / "ib_docker").exists()


def test_ibc_sample_is_generic_and_contains_no_credentials():
    config = _read_ini(WINDOWS_ROOT / "config.sample.ini")

    assert config["IbLoginId"] == ""
    assert config["IbPassword"] == ""
    assert config["OverrideTwsApiPort"] == ""
    assert config["ReloginAfterSecondFactorAuthenticationTimeout"] == "yes"
    assert config["SecondFactorAuthenticationExitInterval"] == "600"
    assert config["AutoRestartTime"] == "08:30 AM"
    assert config["ReadOnlyApi"] == "no"
    assert config["CommandServerPort"] == "0"


def test_windows_launcher_supports_new_and_old_jts_layouts():
    path = WINDOWS_ROOT / "StartGateways.bat"
    raw = path.read_bytes()
    text = raw.decode("utf-8")

    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert 'for /r "%IBC_CONFIG_DIR%" %%F in (config-*.ini)' in text
    assert "IBCWin-*" in text
    assert "IBC_CANDIDATE_COUNT" in text
    assert "%IBC_TWS_PATH%\\ibgateway\\!IBC_SELECTED_VERSION!\\jars" in text
    assert "%IBC_TWS_PATH%\\!IBC_SELECTED_VERSION!\\jars" in text
    assert 'set "TRADING_MODE="' in text
    assert "IBC_RUNNING_COUNT" in text
    assert "IBC_SEEN_!IBC_INSTANCE_KEY!" in text
    assert "7496" not in text
    assert "4001" not in text


def test_sensitive_instance_files_are_ignored_but_samples_are_kept():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "ib_gateway/ibc_windows/**/config-*.ini" in gitignore
    assert "ib_gateway/ibc_windows/settings/" in gitignore
    assert "ib_gateway/ibc_windows/Logs/" in gitignore
    assert "ib_gateway/ib_docker/.env" in gitignore
    assert (WINDOWS_ROOT / "config.sample.ini").is_file()


def test_docker_template_uses_ignored_env_copy():
    compose = (DOCKER_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (DOCKER_ROOT / ".env.example").read_text(encoding="utf-8")
    env_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", env_example, re.MULTILINE))
    compose_keys = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", compose))

    assert not (DOCKER_ROOT / ".env").exists()
    assert compose_keys
    assert compose_keys <= env_keys
    assert "/home/ibgateway/settings" in compose
    assert "/home/ibgateway/Jts:/home/ibgateway/Jts" not in compose
