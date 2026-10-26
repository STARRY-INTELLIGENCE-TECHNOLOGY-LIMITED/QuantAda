"""带每日快照的历史财报日历 Provider。

Nasdaq 日历可作为当前公开日历入口；只有保存了 ``snapshot_date`` 的归档记录，
Provider 才声明支持历史点时查询。没有归档时不把今天抓到的日历冒充历史事实。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

import config
from common.data_paths import ensure_data_dir, resolve_data_path, share_data_file
from common.options.analytics import underlying_key
from .base_provider import BaseDataProvider


class EarningsDataProvider(BaseDataProvider):
    """读取本地点时财报归档，并可抓取短窗口 Nasdaq 当前日历。"""

    PRIORITY = 92
    AUXILIARY_ONLY = True
    EVENT_ONLY = True
    EARNINGS_POINT_IN_TIME = True
    REQUEST_TIMEOUT_SECONDS = 15
    ARCHIVE_TTL_SECONDS = 24 * 60 * 60

    def __init__(self):
        root = Path(resolve_data_path(getattr(config, "DATA_PATH", ".data")))
        default_path = root / "market_cache" / "earnings" / "calendar.csv"
        self.archive_path = Path(str(getattr(config, "EARNINGS_ARCHIVE_PATH", "") or default_path))
        if not self.archive_path.is_absolute():
            self.archive_path = Path(resolve_data_path(self.archive_path))
        ensure_data_dir(self.archive_path.parent)
        self._memory = None

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        """财报事件源不提供 OHLCV。"""
        return None

    def get_earnings_calendar(self, symbols=None, start_date=None, end_date=None, as_of=None):
        """返回包含 ``visible_from`` 的财报事件，严格区分归档和当前抓取。"""
        requested = {_ticker(item) for item in ([symbols] if isinstance(symbols, str) else symbols or [])}
        requested.discard("")
        frame = self._load_archive()
        archive_supported = isinstance(frame, pd.DataFrame) and not frame.empty
        if archive_supported:
            coverage_frame = frame.loc[frame["symbol"].isin(requested)] if requested else frame
            start = _date(start_date)
            end = _date(end_date)
            cutoff = _date(as_of)
            requested_cutoff = cutoff or end or pd.Timestamp.now().normalize()
            if requested:
                coverage = coverage_frame.groupby("symbol")["visible_from"].agg(["min", "max"])
                archive_supported = bool(
                    len(coverage) == len(requested)
                    and coverage["min"].lt(requested_cutoff).all()
                    and coverage["max"].add(pd.Timedelta(days=5)).ge(requested_cutoff).all()
                )
            else:
                coverage_start = coverage_frame["visible_from"].min() if not coverage_frame.empty else pd.NaT
                coverage_end = coverage_frame["visible_from"].max() if not coverage_frame.empty else pd.NaT
                archive_supported = bool(
                    pd.notna(coverage_start) and pd.notna(coverage_end)
                    and coverage_start < requested_cutoff
                    and requested_cutoff <= coverage_end + pd.Timedelta(days=5)
                )
        if archive_supported:
            result = frame.copy()
            if requested:
                result = result.loc[result["symbol"].isin(requested)]
            if start is not None:
                result = result.loc[result["event_date"] >= start]
            if end is not None:
                result = result.loc[result["event_date"] <= end]
            if cutoff is not None:
                result = result.loc[result["visible_from"].lt(cutoff)]
            result = result.reset_index(drop=True)
            result.attrs["source"] = "nasdaq_earnings_archive"
            # 归档只保存有事件的行；空结果不能证明该标的当天没有财报。
            result.attrs["as_of_supported"] = bool(not result.empty)
            return result

        # 只抓取短窗口当前日历，避免一次回测请求触发数千次网页调用。
        start = _date(start_date) or pd.Timestamp.now().normalize()
        end = _date(end_date) or start
        if (end - start).days > 31 or as_of is not None:
            return None
        rows = []
        for day in pd.date_range(start, end, freq="D"):
            payload = self._download_day(day)
            if not isinstance(payload, dict):
                continue
            for item in payload.get("data", {}).get("rows", []) or []:
                ticker = _ticker(item.get("symbol"))
                if requested and ticker not in requested:
                    continue
                rows.append({
                    "symbol": ticker,
                    "event_date": day.normalize(),
                    "event_timestamp": pd.NaT,
                    "period_text": item.get("fiscalQuarterEnding", ""),
                    "visible_from": pd.Timestamp.now().normalize(),
                    "source": "nasdaq_current_calendar",
                })
        if not rows:
            return None
        result = pd.DataFrame(rows)
        result.attrs["source"] = "nasdaq_current_calendar"
        result.attrs["as_of_supported"] = False
        return result

    def capture_current(self, start_date=None, end_date=None, symbols=None):
        """抓取当前公开日历并追加 snapshot_date，供每日任务归档。"""
        result = self.get_earnings_calendar(symbols, start_date, end_date)
        if result is None or result.empty:
            return 0
        rows = result.copy()
        today = pd.Timestamp.now().normalize()
        rows["visible_from"] = today
        rows["snapshot_date"] = today
        self._append_archive(rows)
        return len(rows)

    def _append_archive(self, rows):
        """原子追加每日快照，重复采集保持幂等。"""
        existing = self._load_archive()
        if existing is None:
            existing = pd.DataFrame()
        merged = pd.concat([existing, rows], ignore_index=True, sort=False)
        merged = merged.drop_duplicates(["symbol", "event_date", "visible_from"], keep="last")
        temp_path = self.archive_path.with_suffix(self.archive_path.suffix + ".tmp")
        merged.to_csv(temp_path, index=False)
        temp_path.replace(self.archive_path)
        share_data_file(self.archive_path)
        self._memory = _normalize_archive(merged)

    def _load_archive(self):
        if self._memory is not None:
            return self._memory.copy()
        if not self.archive_path.is_file():
            return None
        try:
            frame = pd.read_csv(self.archive_path)
        except (OSError, ValueError):
            return None
        normalized = _normalize_archive(frame)
        self._memory = normalized.copy()
        return normalized

    def _download_day(self, day):
        query = urlencode({"date": pd.Timestamp(day).strftime("%Y-%m-%d")})
        request = Request(
            f"https://api.nasdaq.com/api/calendar/earnings?{query}",
            headers={
                "User-Agent": "QuantAda/1.0 earnings research",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/market-activity/earnings",
            },
        )
        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
            print(f"[Earnings] Nasdaq calendar request failed: {exc}")
            return None


def _normalize_archive(frame):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=["symbol", "event_date", "event_timestamp", "period_text", "visible_from", "source"])
    columns = {str(column).strip().lower(): column for column in frame.columns}
    def column(*names):
        for name in names:
            if name.lower() in columns:
                return frame[columns[name.lower()]]
        return pd.Series([None] * len(frame), index=frame.index)
    result = pd.DataFrame({
        "symbol": column("symbol", "ticker", "security").map(_ticker),
        "event_date": pd.to_datetime(column("event_date", "earnings_date", "report_date"), errors="coerce").dt.normalize(),
        "event_timestamp": pd.to_datetime(column("event_timestamp", "timestamp"), errors="coerce"),
        "period_text": column("period_text", "period", "fiscal_quarter_ending").fillna("").astype("string"),
        "visible_from": pd.to_datetime(column("visible_from", "snapshot_date", "as_of"), errors="coerce").dt.normalize(),
        "source": column("source", "provider").fillna("earnings_archive").astype("string"),
    })
    result = result.loc[result["symbol"].ne("") & result["event_date"].notna() & result["visible_from"].notna()]
    return result.drop_duplicates(["symbol", "event_date", "visible_from"], keep="last").sort_values(
        ["event_date", "symbol", "visible_from"], kind="stable"
    ).reset_index(drop=True)


def _ticker(value):
    raw = str(value or "").strip().upper()
    if raw.startswith("US."):
        raw = raw[3:]
    return raw if raw and "." not in raw and ":" not in raw else ""


def _date(value):
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).normalize()


__all__ = ["EarningsDataProvider"]
