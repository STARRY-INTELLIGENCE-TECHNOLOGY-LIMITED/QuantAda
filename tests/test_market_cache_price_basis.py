"""行情缓存按价格口径隔离：来源/复权口径不一致、旧缓存无标记、基准变化或不相交时不得拼接或命中。"""

import pandas as pd
import pytest

import config
from data_providers.base_provider import BaseDataProvider
from data_providers.csv_provider import PRICE_BASIS_COLUMN, CsvDataProvider
from data_providers.manager import DataManager


def _frame(dates, closes):
    return pd.DataFrame(
        {
            "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": [100.0] * len(closes),
        },
        index=pd.to_datetime(dates),
    )


def _cache_path(tmp_path, name="US_AAPL.csv"):
    return tmp_path / "market_cache" / name


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path


def _manager(monkeypatch, providers):
    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: list(providers),
    )
    return DataManager()


class _Recording(BaseDataProvider):
    PRIORITY = 1
    frame = None

    def __init__(self):
        self.calls = []

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        self.calls.append((symbol, start_date, end_date))
        return self.frame.copy() if self.frame is not None else None


class GmDataProvider(_Recording):
    adjustment = 1

    def price_adjustment(self):
        return self.adjustment


class IbkrDataProvider(_Recording):
    pass


def test_cache_from_other_source_is_not_served(cache_env, monkeypatch):
    gm = GmDataProvider()
    gm.frame = _frame(["2024-01-02", "2024-01-03"], [10.0, 11.0])
    ibkr = IbkrDataProvider()
    ibkr.frame = _frame(["2024-01-02", "2024-01-03"], [20.0, 21.0])
    manager = _manager(monkeypatch, [gm, ibkr])

    manager.get_data("US.AAPL", "2024-01-02", "2024-01-03", specified_sources="gm")
    assert manager.get_data("US.AAPL", "2024-01-02", "2024-01-03", specified_sources="gm") is not None
    assert len(gm.calls) == 1

    result = manager.get_data("US.AAPL", "2024-01-02", "2024-01-03", specified_sources="ibkr")

    assert len(ibkr.calls) == 1
    assert result["close"].tolist() == [20.0, 21.0]
    assert PRICE_BASIS_COLUMN not in result.columns
    assert result.attrs["price_basis"] == "ibkr"
    cached = pd.read_csv(_cache_path(cache_env), index_col="datetime")
    assert cached[PRICE_BASIS_COLUMN].eq("ibkr").all()


def test_gm_adjustment_change_refetches_and_replaces_cache(cache_env, monkeypatch):
    gm = GmDataProvider()
    gm.frame = _frame(["2024-01-02", "2024-01-03"], [10.0, 11.0])
    manager = _manager(monkeypatch, [gm])
    manager.get_data("US.AAPL", "2024-01-02", "2024-01-03", specified_sources="gm")

    gm.adjustment = 0
    gm.frame = _frame(["2024-01-02", "2024-01-03"], [12.0, 13.0])
    result = manager.get_data("US.AAPL", "2024-01-02", "2024-01-03", specified_sources="gm")

    assert len(gm.calls) == 2
    assert result["close"].tolist() == [12.0, 13.0]
    cached = pd.read_csv(_cache_path(cache_env), index_col="datetime")
    assert cached[PRICE_BASIS_COLUMN].eq("gm:0").all()
    assert cached["close"].tolist() == [12.0, 13.0]


def test_legacy_cache_without_basis_is_refetched_and_replaced(cache_env, monkeypatch):
    path = _cache_path(cache_env)
    path.parent.mkdir(parents=True, exist_ok=True)
    _frame(["2023-12-29", "2024-01-02", "2024-01-03"], [1.0, 2.0, 3.0]).rename_axis("datetime").to_csv(path)
    ibkr = IbkrDataProvider()
    ibkr.frame = _frame(["2024-01-02", "2024-01-03"], [20.0, 21.0])
    manager = _manager(monkeypatch, [ibkr])

    result = manager.get_data("US.AAPL", "2024-01-02", "2024-01-03", specified_sources="ibkr")

    assert len(ibkr.calls) == 1
    assert result["close"].tolist() == [20.0, 21.0]
    cached = pd.read_csv(path, index_col="datetime")
    assert list(cached.index) == ["2024-01-02", "2024-01-03"]


def _seed(manager, frame, basis="gm:1"):
    manager._cache_data(frame, "US.AAPL", price_basis=basis)


def test_forward_adjusted_rebase_replaces_instead_of_splicing(cache_env):
    manager = object.__new__(DataManager)
    manager.data_path = str(cache_env)
    _seed(manager, _frame(["2024-01-02", "2024-01-03", "2024-01-04"], [10.0, 11.0, 12.0]))

    # 分红后前复权基准变化：重叠行价格不同，必须整体替换。
    _seed(manager, _frame(["2024-01-03", "2024-01-04", "2024-01-05"], [10.5, 11.5, 12.5]))

    cached = pd.read_csv(_cache_path(cache_env), index_col="datetime")
    assert list(cached.index) == ["2024-01-03", "2024-01-04", "2024-01-05"]
    assert cached["close"].tolist() == [10.5, 11.5, 12.5]


def test_disjoint_segments_are_not_merged(cache_env):
    manager = object.__new__(DataManager)
    manager.data_path = str(cache_env)
    _seed(manager, _frame(["2024-01-02", "2024-01-03"], [10.0, 11.0]))

    _seed(manager, _frame(["2024-03-01", "2024-03-04"], [20.0, 21.0]))

    cached = pd.read_csv(_cache_path(cache_env), index_col="datetime")
    assert list(cached.index) == ["2024-03-01", "2024-03-04"]


def test_consistent_overlap_still_merges_and_ignores_partial_last_bar(cache_env):
    manager = object.__new__(DataManager)
    manager.data_path = str(cache_env)
    # 旧缓存最后一根可能是盘中快照，与收盘后的终值不同也允许合并。
    _seed(manager, _frame(["2024-01-02", "2024-01-03", "2024-01-04"], [10.0, 11.0, 11.7]))

    _seed(manager, _frame(["2024-01-03", "2024-01-04", "2024-01-05"], [11.0, 12.0, 13.0]))

    cached = pd.read_csv(_cache_path(cache_env), index_col="datetime")
    assert list(cached.index) == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    assert cached["close"].tolist() == [10.0, 11.0, 12.0, 13.0]
    assert cached[PRICE_BASIS_COLUMN].eq("gm:1").all()


def test_csv_provider_strips_basis_column(cache_env):
    manager = object.__new__(DataManager)
    manager.data_path = str(cache_env)
    _seed(manager, _frame(["2024-01-02"], [10.0]))

    frame = CsvDataProvider(str(cache_env)).get_data("US.AAPL")

    assert PRICE_BASIS_COLUMN not in frame.columns
    assert frame.attrs["price_basis"] == "gm:1"


def test_default_chain_option_symbol_does_not_raise(cache_env, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DATA", False)
    provider = IbkrDataProvider()
    provider.frame = _frame(["2024-01-02"], [1.5])
    manager = _manager(monkeypatch, [provider])

    result = manager.get_data("US.AAPL240119P00150000", "2024-01-02", "2024-01-02")

    assert result is not None
    assert result["close"].tolist() == [1.5]


def test_csv_provider_mixed_basis_file_is_unknown_even_for_single_basis_window(cache_env):
    path = _cache_path(cache_env)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = _frame(["2024-01-02", "2024-01-03", "2024-01-04"], [1.0, 2.0, 3.0])
    frame[PRICE_BASIS_COLUMN] = ["gm:1", "gm:1", "gm:0"]
    frame.rename_axis("datetime").to_csv(path)

    window = CsvDataProvider(str(cache_env)).get_data("US.AAPL", "2024-01-02", "2024-01-03")

    assert window.attrs["price_basis"] is None
