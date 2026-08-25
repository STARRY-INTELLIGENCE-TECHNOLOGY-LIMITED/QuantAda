import backtrader as bt
import backtrader.plot as btplot


VALID_PLOT_SCOPES = {'full', 'portfolio', 'portfolio_equity', 'portfolio_drawdown', 'monthly_heatmap'}
PORTFOLIO_PLOT_SCOPES = {'portfolio', 'portfolio_equity', 'portfolio_drawdown'}


class _PlotWithBottomMargin(btplot.Plot):
    """Backtrader 原生绘图器，并为横轴日期增加底部空间。"""

    def __init__(self, bottom_margin=None, force_bottom_xaxis=False, defer_show=False,
                 figid_offset=0, **kwargs):
        self._bottom_margin = bottom_margin
        self._force_bottom_xaxis = force_bottom_xaxis
        self._defer_show = defer_show
        self._figid_offset = figid_offset
        super().__init__(**kwargs)

    def plot(self, *args, **kwargs):
        if self._figid_offset and 'figid' in kwargs:
            kwargs = dict(kwargs)
            kwargs['figid'] += self._figid_offset
        figs = super().plot(*args, **kwargs)
        if self._bottom_margin is not None:
            for fig in figs or []:
                fig.subplots_adjust(bottom=self._bottom_margin)
        if self._force_bottom_xaxis:
            for fig in figs or []:
                self._force_bottom_xaxis_visible(fig)
        return figs

    def show(self):
        if self._defer_show:
            return
        return super().show()

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


def parse_plot_scopes(plot_scope) -> tuple[str, ...]:
    if isinstance(plot_scope, (list, tuple)):
        raw_scopes = plot_scope
    else:
        raw_scopes = str(plot_scope or 'full').split(',')

    scopes = tuple(
        normalize_plot_scope(raw_scope)
        for raw_scope in raw_scopes
        if str(raw_scope).strip()
    ) or ('full',)

    if 'full' in scopes and len(scopes) > 1:
        raise ValueError("plot_scope='full' cannot be combined with other plot scopes.")

    return scopes


def _uses_portfolio_plotting(plot_scope) -> bool:
    scopes = parse_plot_scopes(plot_scope)
    return any(scope in PORTFOLIO_PLOT_SCOPES for scope in scopes)


def create_cerebro(plot_scope: str):
    return bt.Cerebro(stdstats=not _uses_portfolio_plotting(plot_scope))


def configure_plot_observers(cerebro, plot_scope: str) -> None:
    scopes = parse_plot_scopes(plot_scope)
    if not any(scope in PORTFOLIO_PLOT_SCOPES for scope in scopes):
        return

    if any(scope in {'portfolio', 'portfolio_equity'} for scope in scopes):
        cerebro.addobserver(bt.observers.Broker)
    if any(scope in {'portfolio', 'portfolio_drawdown'} for scope in scopes):
        cerebro.addobserver(bt.observers.DrawDown)


def apply_data_feed_plot_scope(feed, plot_scope: str) -> None:
    if _uses_portfolio_plotting(plot_scope):
        feed.plotinfo.plot = False


def _configure_matplotlib_window() -> None:
    try:
        import matplotlib

        # 保留原生 toolbar，便于截图、平移和缩放。
        matplotlib.rcParams['toolbar'] = 'toolbar2'
    except Exception:
        pass


def _set_portfolio_observer_visibility(cerebro, scope: str) -> None:
    show_broker = scope in {'portfolio', 'portfolio_equity'}
    show_drawdown = scope in {'portfolio', 'portfolio_drawdown'}

    for stratlist in getattr(cerebro, 'runstrats', []) or []:
        for strat in stratlist:
            for observer in strat.getobservers():
                if isinstance(observer, bt.observers.Broker):
                    observer.plotinfo.plot = show_broker
                elif isinstance(observer, bt.observers.DrawDown):
                    observer.plotinfo.plot = show_drawdown


def _plot_portfolio_scope(cerebro, scope: str, defer_show: bool, figid_offset: int = 0) -> None:
    _set_portfolio_observer_visibility(cerebro, scope)
    plotter = _PlotWithBottomMargin(
        bottom_margin=0.20 if scope == 'portfolio' else 0.18,
        force_bottom_xaxis=True,
        defer_show=defer_show,
        figid_offset=figid_offset,
        linevalues=False,
        valuetags=False,
        fmt_x_ticks='%Y-%m-%d',
        tickrotation=15,
    )
    cerebro.plot(plotter=plotter)


def _get_first_strategy(cerebro):
    for stratlist in getattr(cerebro, 'runstrats', []) or []:
        for strat in stratlist:
            return strat
    return None


def _plot_monthly_heatmap_scope(cerebro, defer_show: bool, figid_offset: int = 0) -> None:
    import matplotlib.pyplot as plt
    from backtest.report_figures import render_monthly_return_heatmap

    strat = _get_first_strategy(cerebro)
    monthly_returns = {}
    title = "Monthly Return Heatmap"

    if strat is not None:
        try:
            monthly_returns = strat.analyzers.getbyname('timereturn_monthly').get_analysis()
        except Exception:
            monthly_returns = {}

        strategy = getattr(strat, 'strategy', None)
        if strategy is not None:
            title = f"{title} - {strategy.__class__.__name__}"

    render_monthly_return_heatmap(
        monthly_returns,
        title=title,
        fig_num=50000 + figid_offset,
    )

    if not defer_show:
        plt.show()


def plot_cerebro(cerebro, plot_scope: str) -> None:
    _configure_matplotlib_window()
    scopes = parse_plot_scopes(plot_scope)
    if scopes == ('full',):
        cerebro.plot()
        return

    defer_show = len(scopes) > 1
    for index, scope in enumerate(scopes):
        figid_offset = index * 1000
        if scope in PORTFOLIO_PLOT_SCOPES:
            _plot_portfolio_scope(cerebro, scope, defer_show=defer_show, figid_offset=figid_offset)
        elif scope == 'monthly_heatmap':
            _plot_monthly_heatmap_scope(cerebro, defer_show=defer_show, figid_offset=figid_offset)

    if defer_show:
        import matplotlib.pyplot as plt

        plt.show()
