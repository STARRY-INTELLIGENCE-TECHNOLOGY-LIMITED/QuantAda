"""运行时解析配置值的回归测试。"""


def test_csv_provider_resolves_data_path_when_constructed(monkeypatch, tmp_path):
    import data_providers.csv_provider as csv_module

    configured_path = tmp_path / "runtime-data"
    monkeypatch.setattr(csv_module.config, "DATA_PATH", str(configured_path))

    provider = csv_module.CsvDataProvider()

    assert provider.data_path == str(configured_path)
    assert configured_path.is_dir()


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
