import os
import stat

from common.data_paths import ensure_data_dir, resolve_data_path, share_data_file


def test_relative_data_path_anchors_to_run_py_directory(tmp_path, monkeypatch):
    import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "DATA_PATH", ".data")

    resolved = resolve_data_path()

    assert os.path.normcase(resolved) == os.path.normcase(
        str(os.path.join(os.path.dirname(os.path.abspath(config.__file__)), ".data"))
    )
    assert os.path.normcase(resolved) != os.path.normcase(str(tmp_path / ".data"))


def test_absolute_data_path_is_not_rewritten(tmp_path):
    custom = tmp_path / "elsewhere" / "cache"
    assert os.path.normcase(resolve_data_path(str(custom))) == os.path.normcase(str(custom))


def test_ensure_data_dir_uses_wide_mode_for_custom_path(tmp_path, monkeypatch):
    calls = []
    real_chmod = os.chmod

    def record(path, mode):
        calls.append((os.path.normcase(str(path)), mode))
        return real_chmod(path, mode)

    monkeypatch.setattr("common.data_paths.os.chmod", record)
    custom = tmp_path / "custom-data" / "market_cache"
    created = ensure_data_dir(custom)
    file_path = custom / "US_AAPL.csv"
    file_path.write_text("datetime,close\n2024-01-02,1\n", encoding="utf-8")
    share_data_file(file_path)

    assert os.path.normcase(created) == os.path.normcase(os.path.normpath(str(custom)))
    assert custom.is_dir()
    assert (os.path.normcase(str(custom)), 0o777) in calls
    assert (os.path.normcase(str(tmp_path / "custom-data")), 0o777) in calls
    assert (os.path.normcase(str(file_path)), 0o666) in calls
    if os.name != "nt":
        assert stat.S_IMODE(custom.stat().st_mode) == 0o777
        assert stat.S_IMODE(file_path.stat().st_mode) == 0o666
