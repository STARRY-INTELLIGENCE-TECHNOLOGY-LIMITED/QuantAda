from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _coerce_year_month(key):
    if hasattr(key, "year") and hasattr(key, "month"):
        return int(key.year), int(key.month)

    if isinstance(key, (tuple, list)) and len(key) >= 2:
        try:
            return int(key[0]), int(key[1])
        except (TypeError, ValueError):
            pass

    if isinstance(key, (int, float)) and not isinstance(key, bool):
        key_str = str(int(key))
        if len(key_str) == 8:
            ts = pd.to_datetime(key_str, format="%Y%m%d", errors="coerce")
        elif len(key_str) == 6:
            ts = pd.to_datetime(key_str, format="%Y%m", errors="coerce")
        else:
            ts = pd.to_datetime(key_str, errors="coerce")
    else:
        ts = pd.to_datetime(key, errors="coerce")

    if pd.isna(ts):
        return None
    return int(ts.year), int(ts.month)


def build_monthly_return_table(monthly_returns) -> pd.DataFrame:
    records = []
    for key, value in (monthly_returns or {}).items():
        try:
            monthly_return = float(value)
        except (TypeError, ValueError):
            continue

        if not math.isfinite(monthly_return):
            continue

        year_month = _coerce_year_month(key)
        if year_month is None:
            continue

        year, month = year_month
        records.append({
            "year": year,
            "month": month,
            "monthly_return": monthly_return,
        })

    if not records:
        return pd.DataFrame(columns=range(1, 13))

    df = pd.DataFrame(records)
    table = df.pivot(index="year", columns="month", values="monthly_return")
    table = table.sort_index()
    table = table.reindex(columns=range(1, 13))
    table.index.name = "Year"
    table.columns.name = "Month"
    return table


def render_monthly_return_heatmap(monthly_returns, title=None, fig_num=None):
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm

    table = build_monthly_return_table(monthly_returns)

    if table.empty:
        fig = plt.figure(num=fig_num, figsize=(10, 3.5), clear=True) if fig_num is not None else plt.figure(figsize=(10, 3.5), clear=True)
        ax = fig.add_subplot(111)
        ax.axis("off")
        ax.text(
            0.5,
            0.5,
            "No monthly return data available",
            ha="center",
            va="center",
            fontsize=12,
        )
        if title:
            fig.suptitle(title, fontsize=14, y=0.96)
        fig.tight_layout()
        return fig

    display_table = table * 100.0
    values = display_table.to_numpy(dtype=float)
    finite_values = values[np.isfinite(values)]
    if finite_values.size:
        vmax = float(np.max(np.abs(finite_values)))
    else:
        vmax = 1.0
    if vmax <= 0:
        vmax = 1.0

    masked = np.ma.masked_invalid(values)
    cmap = plt.get_cmap("RdYlGn").copy()
    cmap.set_bad(color="#f2f2f2")
    norm = TwoSlopeNorm(vcenter=0.0, vmin=-vmax, vmax=vmax)

    height = max(3.8, 0.55 * len(display_table.index) + 1.8)
    if fig_num is None:
        fig = plt.figure(figsize=(12.5, height), clear=True)
    else:
        fig = plt.figure(num=fig_num, figsize=(12.5, height), clear=True)
    ax = fig.add_subplot(111)
    im = ax.imshow(masked, cmap=cmap, norm=norm, aspect="auto")

    ax.set_xticks(np.arange(len(MONTH_LABELS)))
    ax.set_xticklabels(MONTH_LABELS)
    ax.set_yticks(np.arange(len(display_table.index)))
    ax.set_yticklabels([str(int(year)) for year in display_table.index])
    ax.set_xlabel("Month")
    ax.set_ylabel("Year")

    ax.set_xticks(np.arange(-0.5, len(MONTH_LABELS), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(display_table.index), 1), minor=True)
    ax.grid(which="minor", color="white", linestyle="-", linewidth=1.0)
    ax.tick_params(which="minor", bottom=False, left=False)

    for row_idx, year in enumerate(display_table.index):
        for col_idx, month in enumerate(range(1, 13)):
            value = display_table.loc[year, month]
            if pd.isna(value):
                continue
            text_color = "white" if abs(value) >= vmax * 0.55 else "#222222"
            ax.text(
                col_idx,
                row_idx,
                f"{value:.1f}%",
                ha="center",
                va="center",
                fontsize=8.5,
                color=text_color,
            )

    if title:
        ax.set_title(title, fontsize=14, pad=14)

    cbar = fig.colorbar(im, ax=ax, fraction=0.032, pad=0.02)
    cbar.set_label("Monthly Return (%)")

    fig.tight_layout()
    return fig


def save_monthly_return_heatmap(monthly_returns, output_path, title=None):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    fig = render_monthly_return_heatmap(monthly_returns, title=title)
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return output_path
