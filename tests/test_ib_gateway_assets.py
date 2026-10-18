import os
import re
import subprocess
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
    assert 'cd /d "%IBC_SCRIPT_DIR%"' in text
    assert 'cd /d "%IBC_PATH%"' in text
    assert 'cd /d "!IBC_PATH!"' in text
    assert 'cd /d "%TWS_SETTINGS_PATH%"' in text
    assert '/D "!IBC_PATH!"' in text
    assert "IBCWin-*.zip" in text
    assert "does not use the caller working directory" in text
    assert "7496" not in text
    assert "4001" not in text

    wrapper = (WINDOWS_ROOT / "StartGateway.bat").read_bytes()
    creator = (WINDOWS_ROOT / "CreateDesktopShortcut.bat").read_bytes()
    assert b"\r\n" in wrapper and b"\n" not in wrapper.replace(b"\r\n", b"")
    assert b"\r\n" in creator and b"\n" not in creator.replace(b"\r\n", b"")
    wrapper_text = wrapper.decode("utf-8")
    creator_text = creator.decode("utf-8")
    assert 'cd /d "%IBC_SCRIPT_DIR%"' in wrapper_text
    assert 'call "%IBC_SCRIPT_DIR%\\StartGateways.bat"' in wrapper_text
    assert "WorkingDirectory=$env:IBC_SCRIPT_DIR" in creator_text
    assert "TargetPath=$env:IBC_LAUNCHER" in creator_text
    assert "Desktop\\StartGateway.lnk" in creator_text


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


def _launcher_env(jts, config, marker):
    env = os.environ.copy()
    for key in (
        "IBC_PATH",
        "IBC_SETTINGS_ROOT",
        "IBC_LOG_ROOT",
        "IBC_TWS_MAJOR_VRSN",
        "TWS_MAJOR_VRSN",
        "IBC_DRY_RUN",
        "IBC_STUB_MARKER",
    ):
        env.pop(key, None)
    env["IBC_TWS_PATH"] = str(jts)
    env["IBC_CONFIG_DIR"] = str(config)
    env["IBC_STUB_MARKER"] = str(marker)
    return env


def _run_bat(bat, cwd, env, *args):
    return subprocess.run(
        [env.get("ComSpec", "cmd.exe"), "/d", "/c", "call", str(bat), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _prepare_desktop(tmp_path):
    desktop = tmp_path / "Desktop"
    ibc = desktop / "IBCWin-3.20260922.24.2"
    scripts = ibc / "scripts"
    scripts.mkdir(parents=True)
    stub = (
        "@echo off\r\n"
        '> "%IBC_STUB_MARKER%" echo %CD%\r\n'
        "mkdir relative-leak\r\n"
        "exit /b 0\r\n"
    )
    (scripts / "DisplayBannerAndLaunch.bat").write_bytes(stub.encode("ascii"))
    (ibc / "version").write_bytes(b"3.24.2\r\n")
    wrap = desktop / "ibc_windows"
    wrap.mkdir()
    for name in ("StartGateway.bat", "StartGateways.bat", "CreateDesktopShortcut.bat"):
        (wrap / name).write_bytes((WINDOWS_ROOT / name).read_bytes())
    jts = tmp_path / "Jts"
    (jts / "ibgateway" / "1045" / "jars").mkdir(parents=True)
    config = tmp_path / "configs"
    config.mkdir()
    (config / "config-live.ini").write_text("TradingMode=paper\nIbLoginId=\nIbPassword=\n", encoding="ascii")
    return desktop, ibc, wrap, jts, config


def _assert_no_extra_desktop_dirs(desktop, allowed):
    actual = {path.name for path in desktop.iterdir()}
    assert actual == allowed
    assert not (desktop / "relative-leak").exists()


def test_windows_launcher_pins_shortcut_workdir(tmp_path):
    """桌面快捷方式起始位置为空时，启动不得在桌面留下新的 IBCWin 目录。"""
    desktop, ibc, wrap, jts, config = _prepare_desktop(tmp_path)
    marker = tmp_path / "cwd.txt"
    allowed = {"IBCWin-3.20260922.24.2", "ibc_windows"}
    env = _launcher_env(jts, config, marker)

    dry = _run_bat(wrap / "StartGateways.bat", desktop, env, "/DRYRUN")
    assert dry.returncode == 0, dry.stdout.decode("utf-8", "replace") + dry.stderr.decode("utf-8", "replace")
    assert (ibc / "settings" / "live").is_dir()
    assert not (desktop / "settings").exists()
    _assert_no_extra_desktop_dirs(desktop, allowed)

    wrapped = _run_bat(wrap / "StartGateway.bat", desktop, env, "/DRYRUN")
    assert wrapped.returncode == 0, wrapped.stdout.decode("utf-8", "replace") + wrapped.stderr.decode("utf-8", "replace")
    _assert_no_extra_desktop_dirs(desktop, allowed)

    settings = ibc / "settings" / "live"
    env["IBC_PATH"] = str(ibc)
    instance = _run_bat(
        wrap / "StartGateways.bat",
        desktop,
        env,
        "--instance",
        "live",
        str(config / "config-live.ini"),
        str(settings),
        str(ibc / "Logs" / "live"),
        "1045",
        str(jts),
    )
    assert instance.returncode == 0, instance.stdout.decode("utf-8", "replace") + instance.stderr.decode("utf-8", "replace")
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8", errors="replace").strip().lower() == str(settings).lower()
    assert (settings / "relative-leak").is_dir()
    _assert_no_extra_desktop_dirs(desktop, allowed)


def test_create_desktop_shortcut_pins_working_directory(tmp_path):
    """重建的桌面快捷方式必须指向已解压脚本，且起始位置不是桌面。"""
    home = tmp_path / "home"
    desktop = home / "Desktop"
    install = tmp_path / "ibc_windows"
    desktop.mkdir(parents=True)
    install.mkdir()
    for name in ("StartGateway.bat", "StartGateways.bat", "CreateDesktopShortcut.bat"):
        (install / name).write_bytes((WINDOWS_ROOT / name).read_bytes())
    env = os.environ.copy()
    env["USERPROFILE"] = str(home)
    completed = _run_bat(install / "CreateDesktopShortcut.bat", tmp_path, env)
    assert completed.returncode == 0, completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
    link = desktop / "StartGateway.lnk"
    assert link.is_file()
    assert not any(path.name.startswith("IBCWin-") for path in desktop.iterdir())

    probe = subprocess.run(
        [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-Command",
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:IBC_LINK); Write-Output $s.TargetPath; Write-Output $s.WorkingDirectory",
        ],
        env={**env, "IBC_LINK": str(link)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    assert probe.returncode == 0, probe.stderr
    assert lines[0].lower() == str(install / "StartGateway.bat").lower()
    assert lines[1].lower() == str(install).lower()
    assert lines[1].lower() != str(desktop).lower()
