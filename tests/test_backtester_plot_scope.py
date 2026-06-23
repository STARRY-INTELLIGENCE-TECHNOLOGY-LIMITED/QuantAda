import backtrader as backtrader
import pandas as pd
import pytest

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


def test_invalid_plot_scope_is_rejected():
    with pytest.raises(ValueError, match="Invalid plot_scope"):
        Backtester(
            datas={"AAA": _make_df()},
            strategy_class=_NoopStrategy,
            plot_scope="signals",
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
