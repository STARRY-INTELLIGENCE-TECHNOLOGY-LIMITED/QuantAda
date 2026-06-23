import backtrader as bt
import backtrader.plot as btplot


VALID_PLOT_SCOPES = {'full', 'portfolio'}


class _PlotWithBottomMargin(btplot.Plot):
    """Backtrader-native plotter with extra bottom space for x-axis dates."""

    def __init__(self, bottom_margin=None, force_bottom_xaxis=False, **kwargs):
        self._bottom_margin = bottom_margin
        self._force_bottom_xaxis = force_bottom_xaxis
        super().__init__(**kwargs)

    def plot(self, *args, **kwargs):
        figs = super().plot(*args, **kwargs)
        if self._bottom_margin is not None:
            for fig in figs or []:
                fig.subplots_adjust(bottom=self._bottom_margin)
        if self._force_bottom_xaxis:
            for fig in figs or []:
                self._force_bottom_xaxis_visible(fig)
        return figs

    @staticmethod
    def _force_bottom_xaxis_visible(fig):
        axes = [ax for ax in fig.axes if ax.get_visible()]
        if not axes:
            return

        min_y0 = min(ax.get_position().y0 for ax in axes)
        bottom_axes = [ax for ax in axes if abs(ax.get_position().y0 - min_y0) < 1e-6]

        for ax in bottom_axes:
            ax.tick_params(axis='x', which='both', bottom=True, labelbottom=True)
            ax.set_xlabel('')
            for label in ax.get_xticklabels():
                label.set_visible(True)


def normalize_plot_scope(plot_scope: str) -> str:
    scope = str(plot_scope or 'full').strip().lower()
    if scope not in VALID_PLOT_SCOPES:
        raise ValueError(
            f"Invalid plot_scope={plot_scope!r}. Expected one of: {sorted(VALID_PLOT_SCOPES)}."
        )
    return scope


def create_cerebro(plot_scope: str):
    scope = normalize_plot_scope(plot_scope)
    return bt.Cerebro(stdstats=scope != 'portfolio')


def configure_plot_observers(cerebro, plot_scope: str) -> None:
    if normalize_plot_scope(plot_scope) != 'portfolio':
        return
    cerebro.addobserver(bt.observers.Broker)
    cerebro.addobserver(bt.observers.DrawDown)


def apply_data_feed_plot_scope(feed, plot_scope: str) -> None:
    if normalize_plot_scope(plot_scope) == 'portfolio':
        feed.plotinfo.plot = False


def _configure_matplotlib_window() -> None:
    try:
        import matplotlib

        # Keep the native toolbar visible for screenshots, pan and zoom.
        matplotlib.rcParams['toolbar'] = 'toolbar2'
    except Exception:
        pass


def plot_cerebro(cerebro, plot_scope: str) -> None:
    _configure_matplotlib_window()
    scope = normalize_plot_scope(plot_scope)
    if scope != 'portfolio':
        cerebro.plot()
        return

    plotter = _PlotWithBottomMargin(
        bottom_margin=0.20,
        force_bottom_xaxis=True,
        linevalues=False,
        valuetags=False,
        fmt_x_ticks='%Y-%m-%d',
        tickrotation=15,
    )
    cerebro.plot(plotter=plotter)
