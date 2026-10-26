"""美国财政部日收益率曲线 Provider。"""

from __future__ import annotations

import io
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

import config
from common.data_paths import ensure_data_dir, resolve_data_path, share_data_file
from .base_provider import BaseDataProvider


class RatesDataProvider(BaseDataProvider):
    """读取 Treasury Daily Treasury Par Yield Curve，输出小数利率。"""

    PRIORITY = 93
    AUXILIARY_ONLY = True
    RATE_CURVE = True
    REQUEST_TIMEOUT_SECONDS = 15
    CACHE_TTL_SECONDS = 24 * 60 * 60
    _TENORS = {"1 Mo": "rate_1m", "3 Mo": "rate_3m", "6 Mo": "rate_6m", "1 Yr": "rate_1y", "2 Yr": "rate_2y"}

    def __init__(self):
        root = resolve_data_path(getattr(config, "DATA_PATH", ".data"))
        self.cache_dir = Path(ensure_data_dir(Path(root) / "market_cache" / "rates"))
        self._memory = {}

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        """利率是辅助事实，不伪造可交易 OHLCV。"""
        return None

    @staticmethod
    def supports_symbol(symbol):
        return str(symbol or "").strip().upper() in {"US.RF", "US.SOFR", "RF", "SOFR"}

    def get_rate_curve(self, start_date=None, end_date=None, as_of=None, deadline=None):
        """读取请求区间内的日收益率曲线；缺网时只使用完整本地缓存。"""
        start = pd.Timestamp(start_date).normalize() if start_date else pd.Timestamp("2000-01-01")
        end = pd.Timestamp(end_date).normalize() if end_date else pd.Timestamp.now().normalize()
        cutoff = pd.Timestamp(as_of).normalize() if as_of else end
        end = min(end, cutoff)
        if end < start:
            return pd.DataFrame(columns=["date", "risk_free_rate"])
        frames = [self._load_year(year, deadline=deadline) for year in range(start.year, end.year + 1)]
        frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
        if not frames:
            return None
        result = pd.concat(frames, ignore_index=True, sort=False)
        result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
        result = result.loc[result["date"].between(start, end)].drop_duplicates("date", keep="last")
        result = result.sort_values("date").reset_index(drop=True)
        result.attrs["source"] = "us_treasury_daily_yield_curve"
        result.attrs["as_of_supported"] = bool(as_of is None or result.empty or result["date"].max() <= cutoff)
        return result

    def _load_year(self, year, deadline=None):
        if year in self._memory:
            return self._memory[year].copy()
        path = self.cache_dir / f"treasury_curve_{int(year)}.csv"
        frame = None
        stale_frame = None
        if path.is_file() and time.time() - path.stat().st_mtime <= self.CACHE_TTL_SECONDS:
            try:
                frame = pd.read_csv(path)
            except (OSError, ValueError):
                frame = None
        elif path.is_file():
            try:
                stale_frame = pd.read_csv(path)
            except (OSError, ValueError):
                stale_frame = None
        downloaded = False
        if frame is None:
            frame = (
                self._download_year(year, deadline=deadline)
                if deadline is not None else self._download_year(year)
            )
            downloaded = frame is not None and not frame.empty
            if (frame is None or frame.empty) and stale_frame is not None:
                frame = stale_frame
            if downloaded and frame is not None and not frame.empty:
                try:
                    frame.to_csv(path, index=False)
                    share_data_file(path)
                except OSError:
                    pass
        if frame is None or frame.empty:
            return None
        normalized = self._normalize(frame)
        self._memory[year] = normalized.copy()
        return normalized

    def _download_year(self, year, deadline=None):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/{int(year)}/all?type=daily_treasury_yield_curve"
            f"&field_tdr_date_value={int(year)}"
        )
        request = Request(url, headers={"User-Agent": "QuantAda/1.0 rates research"})
        timeout = float(self.REQUEST_TIMEOUT_SECONDS)
        if deadline is not None:
            try:
                timeout = min(timeout, max(0.1, float(deadline) - time.monotonic()))
            except (TypeError, ValueError, OverflowError):
                timeout = float(self.REQUEST_TIMEOUT_SECONDS)
            if timeout <= 0.1:
                return None
        try:
            with urlopen(request, timeout=timeout) as response:
                return pd.read_csv(io.BytesIO(response.read()))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"[Rates] Treasury {year} request failed: {exc}")
            return None

    @classmethod
    def _normalize(cls, frame):
        columns = {str(column).strip(): column for column in frame.columns}
        date_column = columns.get("Date") or columns.get("date")
        if date_column is None:
            return pd.DataFrame()
        result = pd.DataFrame({"date": pd.to_datetime(frame[date_column], errors="coerce")})
        for source, target in cls._TENORS.items():
            source_column = columns.get(source)
            cached_column = columns.get(target)
            if source_column:
                result[target] = pd.to_numeric(frame[source_column], errors="coerce") / 100.0
            elif cached_column:
                result[target] = pd.to_numeric(frame[cached_column], errors="coerce")
            else:
                result[target] = float("nan")
        preferred = result["rate_3m"].where(result["rate_3m"].notna(), result["rate_1m"])
        result["risk_free_rate"] = preferred
        return result.dropna(subset=["date", "risk_free_rate"]).sort_values("date").reset_index(drop=True)


__all__ = ["RatesDataProvider"]
