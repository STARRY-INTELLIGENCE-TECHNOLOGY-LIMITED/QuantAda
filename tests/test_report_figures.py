import pandas as pd

from backtest.report_figures import (
    build_monthly_return_table,
    render_monthly_return_heatmap,
    save_monthly_return_heatmap,
)


def test_build_monthly_return_table_pivots_year_month():
    table = build_monthly_return_table(
        {
            "2024-01-31": 0.10,
            "2024-02-29": -0.05,
            "2025-01-31": 0.20,
        }
    )

    assert list(table.index) == [2024, 2025]
    assert table.loc[2024, 1] == 0.10
    assert table.loc[2024, 2] == -0.05
    assert pd.isna(table.loc[2024, 3])
    assert table.loc[2025, 1] == 0.20


def test_save_monthly_return_heatmap_writes_file(tmp_path):
    output = tmp_path / "figures" / "monthly_heatmap.png"

    result = save_monthly_return_heatmap(
        {
            "2024-01-31": 0.10,
            "2024-02-29": -0.05,
        },
        output,
        title="Monthly Return Heatmap",
    )

    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0


def test_render_monthly_return_heatmap_returns_figure():
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    fig = render_monthly_return_heatmap(
        {
            "2024-01-31": 0.10,
            "2024-02-29": -0.05,
        },
        title="Monthly Return Heatmap",
    )

    assert fig.axes
    plt.close(fig)
