import backtrader as backtrader
import pandas as pd
import pytest

import backtest.plotting as plotting
from backtest.backtester import Backtester
from backtest.plotting import _PlotWithBottomMargin
from strategies.base_strategy import BaseStrategy


class _NoopStrategy(BaseStrategy):
    def init(self):
        pass

    def next(self):
        pass


def _make_df():
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [100.0, 101.0, 102.0],
            "low": [100.0, 101.0, 102.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [1.0, 1.0, 1.0],
        },
        index=idx,
    )


def test_default_plot_scope_keeps_data_feeds_plottable():
    bt = Backtester(
        datas={"AAA": _make_df(), "BBB": _make_df()},
        strategy_class=_NoopStrategy,
        enable_plot=True,
        verbose=False,
    )

    bt._init_data_feeds()

    assert [data.plotinfo.plot for data in bt.cerebro.datas] == [True, True]
    assert bt.cerebro.p.stdstats is True


def test_portfolio_plot_scope_hides_data_feed_price_plots():
    bt = Backtester(
        datas={"AAA": _make_df(), "BBB": _make_df()},
        strategy_class=_NoopStrategy,
        enable_plot=True,
        verbose=False,
        plot_scope="portfolio",
    )

    bt._init_data_feeds()

    assert [data.plotinfo.plot for data in bt.cerebro.datas] == [False, False]
    assert bt.cerebro.p.stdstats is False
    observer_classes = [observer[1] for observer in bt.cerebro.observers]
    assert observer_classes == [backtrader.observers.Broker, backtrader.observers.DrawDown]


@pytest.mark.parametrize(
    ("plot_scope", "expected_observers"),
    [
        ("portfolio_equity", [backtrader.observers.Broker]),
        ("portfolio_drawdown", [backtrader.observers.DrawDown]),
    ],
)
def test_single_portfolio_plot_scopes_use_one_observer(plot_scope, expected_observers):
    bt = Backtester(
        datas={"AAA": _make_df(), "BBB": _make_df()},
        strategy_class=_NoopStrategy,
        enable_plot=True,
        verbose=False,
        plot_scope=plot_scope,
    )

    bt._init_data_feeds()

    assert [data.plotinfo.plot for data in bt.cerebro.datas] == [False, False]
    assert bt.cerebro.p.stdstats is False
    observer_classes = [observer[1] for observer in bt.cerebro.observers]
    assert observer_classes == expected_observers


def test_monthly_heatmap_plot_scope_is_valid_without_portfolio_observers():
    bt = Backtester(
        datas={"AAA": _make_df(), "BBB": _make_df()},
        strategy_class=_NoopStrategy,
        enable_plot=True,
        verbose=False,
        plot_scope="monthly_heatmap",
    )

    bt._init_data_feeds()

    assert bt.plot_scope == ("monthly_heatmap",)
    assert [data.plotinfo.plot for data in bt.cerebro.datas] == [True, True]
    assert bt.cerebro.p.stdstats is True
    assert bt.cerebro.observers == []


def test_multiple_portfolio_plot_scopes_share_one_backtester_result():
    bt = Backtester(
        datas={"AAA": _make_df(), "BBB": _make_df()},
        strategy_class=_NoopStrategy,
        enable_plot=True,
        verbose=False,
        plot_scope="portfolio_equity,portfolio_drawdown",
    )

    bt._init_data_feeds()

    assert bt.plot_scope == ("portfolio_equity", "portfolio_drawdown")
    assert [data.plotinfo.plot for data in bt.cerebro.datas] == [False, False]
    assert bt.cerebro.p.stdstats is False
    observer_classes = [observer[1] for observer in bt.cerebro.observers]
    assert observer_classes == [backtrader.observers.Broker, backtrader.observers.DrawDown]


def test_plot_cerebro_opens_multiple_portfolio_windows_from_same_cerebro(monkeypatch):
    calls = []
    cerebro = object()

    monkeypatch.setattr(plotting, "_configure_matplotlib_window", lambda: None)
    monkeypatch.setattr(
        plotting,
        "_plot_portfolio_scope",
        lambda cerebro_arg, scope, defer_show, figid_offset=0: calls.append(
            (cerebro_arg, scope, defer_show, figid_offset)
        ),
    )

    import matplotlib.pyplot as plt

    monkeypatch.setattr(plt, "show", lambda: calls.append(("show", None, None)))

    plotting.plot_cerebro(cerebro, "portfolio_equity,portfolio_drawdown")

    assert calls == [
        (cerebro, "portfolio_equity", True, 0),
        (cerebro, "portfolio_drawdown", True, 1000),
        ("show", None, None),
    ]


def test_plot_cerebro_opens_monthly_heatmap_with_other_scopes(monkeypatch):
    calls = []
    cerebro = object()

    monkeypatch.setattr(plotting, "_configure_matplotlib_window", lambda: None)
    monkeypatch.setattr(
        plotting,
        "_plot_portfolio_scope",
        lambda cerebro_arg, scope, defer_show, figid_offset=0: calls.append(
            ("portfolio", cerebro_arg, scope, defer_show, figid_offset)
        ),
    )
    monkeypatch.setattr(
        plotting,
        "_plot_monthly_heatmap_scope",
        lambda cerebro_arg, defer_show, figid_offset=0: calls.append(
            ("monthly_heatmap", cerebro_arg, defer_show, figid_offset)
        ),
    )

    import matplotlib.pyplot as plt

    monkeypatch.setattr(plt, "show", lambda: calls.append(("show", None, None)))

    plotting.plot_cerebro(cerebro, "portfolio_equity,monthly_heatmap")

    assert calls == [
        ("portfolio", cerebro, "portfolio_equity", True, 0),
        ("monthly_heatmap", cerebro, True, 1000),
        ("show", None, None),
    ]


def test_plotter_applies_figid_offset(monkeypatch):
    captured = {}

    def fake_plot(self, *args, **kwargs):
        captured["figid"] = kwargs["figid"]
        return []

    monkeypatch.setattr(plotting.btplot.Plot, "plot", fake_plot)

    plotter = _PlotWithBottomMargin(figid_offset=1000)
    plotter.plot(object(), figid=5)

    assert captured["figid"] == 1005


def test_invalid_plot_scope_is_rejected():
    with pytest.raises(ValueError, match="Invalid plot_scope"):
        Backtester(
            datas={"AAA": _make_df()},
            strategy_class=_NoopStrategy,
            plot_scope="signals",
        )


def test_full_plot_scope_cannot_be_combined():
    with pytest.raises(ValueError, match="cannot be combined"):
        Backtester(
            datas={"AAA": _make_df()},
            strategy_class=_NoopStrategy,
            plot_scope="full,portfolio_equity",
        )


def test_force_bottom_xaxis_visible_restores_hidden_bottom_labels():
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(nrows=2, ncols=1)
    for ax in axes:
        ax.plot([0, 1, 2], [1, 2, 3])
        ax.tick_params(axis="x", labelbottom=False)
        for label in ax.get_xticklabels():
            label.set_visible(False)

    _PlotWithBottomMargin._force_bottom_xaxis_visible(fig)

    assert axes[-1].get_xlabel() == ""
    assert any(label.get_visible() for label in axes[-1].get_xticklabels())
    assert not any(label.get_visible() for label in axes[0].get_xticklabels())

    plt.close(fig)
