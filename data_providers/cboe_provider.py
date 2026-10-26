"""CBOE 官方日频指数 CSV 数据源。

本 Provider 只负责 CBOE 指数历史行情的下载和标准化，不负责期权链、下单或实时盘口。
代码映射采用固定白名单，避免把网页 slug、ETF 代理或任意 URL 当作指数行情。
同一 Provider 实例每天按指数代码最多下载一次，后续请求复用内存数据并交给
DataManager 的缓存层持久化。
"""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from common.options.data_safety import sanitize_market_dataframe
from .base_provider import BaseDataProvider


class CboeDataProvider(BaseDataProvider):
    """按 ``US.<CODE>`` 读取 CBOE 官方日频指数 CSV。"""

    PRIORITY = 75
    URL_TEMPLATE = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{filename}"
    # 保留 VIX 旧调用方使用的常量，同时所有新代码应使用代码映射。
    URL = URL_TEMPLATE.format(filename="VIX_History.csv")
    REQUEST_TIMEOUT_SECONDS = 20
    SOURCE_TIMEZONE = ZoneInfo("America/New_York")

    # 这些文件含有官方日频 OHLC；代码是 QuantAda 对外使用的规范代码。
    OHLC_FILES = {
        "VIX": "VIX_History.csv",
        "VIX9D": "VIX9D_History.csv",
        "VIX3M": "VIX3M_History.csv",
        "VIX6M": "VIX6M_History.csv",
        "VIX1Y": "VIX1Y_History.csv",
        "VXN": "VXN_History.csv",
        "RVX": "RVX_History.csv",
        "VXD": "VXD_History.csv",
        "VXEEM": "VXEEM_History.csv",
        "VXEFA": "VXEFA_History.csv",
        "VXAPL": "VXAPL_History.csv",
        "VXAZN": "VXAZN_History.csv",
        "VXGDX": "VXGDX_History.csv",
        "VXGOG": "VXGOG_History.csv",
        "VXGS": "VXGS_History.csv",
        "VXIBM": "VXIBM_History.csv",
    }

    # 这些官方文件只有 DATE 与指数收盘值；标准 OHLCV 接口中的开高低为同一
    # 日终指数值，并在 attrs 中明确标记为 close-only，不能解释为盘中交易区间。
    CLOSE_ONLY_FILES = {
        "VVIX": ("VVIX_History.csv", "VVIX"),
        "OVX": ("OVX_History.csv", "OVX"),
        "GVZ": ("GVZ_History.csv", "GVZ"),
        "SKEW": ("SKEW_History.csv", "SKEW"),
    }
    VIX_SYMBOLS = {"US.VIX", "VIX", "VIX.US"}
    SUPPORTED_CODES = frozenset((*OHLC_FILES, *CLOSE_ONLY_FILES))

    def __init__(self):
        self._cache_lock = threading.Lock()
        self._cached_frames: dict[str, pd.DataFrame | None] = {}
        self._cached_fetch_days: dict[str, date] = {}
        self._fallback_notices: set[str] = set()

    @classmethod
    def _normalize_code(cls, symbol: str) -> str | None:
        """把 ``US.CODE``、``CODE.US`` 和兼容的裸代码转换为 CBOE 代码。"""

        raw = str(symbol or "").strip().upper()
        if raw.startswith("US.") or raw.startswith("CBOE."):
            raw = raw.split(".", 1)[1]
        elif raw.endswith(".US"):
            raw = raw[:-3]
        return raw if raw in cls.SUPPORTED_CODES else None

    @classmethod
    def _is_vix(cls, symbol: str) -> bool:
        """兼容旧调用方，判断代码是否为主 VIX 指数。"""

        return cls._normalize_code(symbol) == "VIX"

    def supports_symbol(self, symbol: str) -> bool:
        """声明本 Provider 固定白名单覆盖范围，供组合源跳过无关取数。"""
        return self._normalize_code(symbol) is not None

    @classmethod
    def supported_symbols(cls) -> tuple[str, ...]:
        """返回文档和选股器应使用的规范代码。"""

        return tuple(f"US.{code}" for code in sorted(cls.SUPPORTED_CODES))

    @classmethod
    def _download(cls, code: str = "VIX") -> pd.DataFrame | None:
        if code in cls.OHLC_FILES:
            filename = cls.OHLC_FILES[code]
            value_kind = "ohlc"
            value_column = None
        else:
            filename, value_column = cls.CLOSE_ONLY_FILES[code]
            value_kind = "close_only"
        url = cls.URL_TEMPLATE.format(filename=filename)
        request = Request(
            url,
            headers={
                "User-Agent": "QuantAda/1.0 CBOE daily index client",
                "Accept": "text/csv,*/*;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=cls.REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"[CBOE] {code} CSV request failed: {exc}")
            return None
        try:
            raw = pd.read_csv(BytesIO(payload))
            columns = {
                str(column).lstrip("\ufeff").strip().upper(): column
                for column in raw.columns
            }
            if "DATE" not in columns:
                print(f"[CBOE] {code} CSV is missing DATE column.")
                return None
            index = pd.to_datetime(raw[columns["DATE"]], errors="coerce")
            if value_kind == "ohlc":
                required = {"OPEN", "HIGH", "LOW", "CLOSE"}
                if not required.issubset(columns):
                    print(f"[CBOE] {code} CSV is missing OHLC columns.")
                    return None
                frame = pd.DataFrame(
                    {
                        "open": pd.to_numeric(raw[columns["OPEN"]], errors="coerce").to_numpy(),
                        "high": pd.to_numeric(raw[columns["HIGH"]], errors="coerce").to_numpy(),
                        "low": pd.to_numeric(raw[columns["LOW"]], errors="coerce").to_numpy(),
                        "close": pd.to_numeric(raw[columns["CLOSE"]], errors="coerce").to_numpy(),
                    },
                    index=index,
                )
            else:
                if value_column.upper() not in columns:
                    print(f"[CBOE] {code} CSV is missing {value_column} column.")
                    return None
                close = pd.to_numeric(raw[columns[value_column.upper()]], errors="coerce").to_numpy()
                frame = pd.DataFrame(
                    {
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                    },
                    index=index,
                )
            frame.index.name = "datetime"
            frame["volume"] = 0.0
            frame = frame.dropna(subset=["open", "high", "low", "close"])
            frame = frame[~frame.index.isna()]
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            frame = sanitize_market_dataframe(frame, require_ohlcv=True)
            if frame is not None and not frame.empty:
                frame.attrs["cboe_code"] = code
                frame.attrs["cboe_file"] = filename
                frame.attrs["cboe_value_kind"] = value_kind
                if value_kind == "close_only":
                    frame.attrs["cboe_ohlc_derived_from_close"] = True
                    # 额外列会随 DataManager CSV 缓存保留，避免 attrs 在跨进程缓存后丢失语义。
                    frame["cboe_value_kind"] = value_kind
                    frame["cboe_ohlc_derived_from_close"] = True
            return frame
        except Exception as exc:
            print(f"[CBOE] {code} CSV normalization failed: {exc}")
            return None

    def _frame_for_today(self, code: str) -> pd.DataFrame | None:
        # CBOE 按美东交易日更新；不能用服务器本地日期，否则亚洲服务器跨午夜后
        # 可能在新数据发布前锁住整天的旧缓存。
        fetch_day = datetime.now(self.SOURCE_TIMEZONE).date()
        with self._cache_lock:
            if self._cached_fetch_days.get(code) == fetch_day:
                return self._cached_frames.get(code)
            frame = self._download(code)
            self._cached_fetch_days[code] = fetch_day
            self._cached_frames[code] = frame
            return frame

    def get_data(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
        timeframe: str = "Days",
        compression: int = 1,
    ) -> pd.DataFrame | None:
        code = self._normalize_code(symbol)
        if code is None:
            return None
        try:
            normalized_compression = int(compression or 1)
        except (TypeError, ValueError):
            normalized_compression = -1
        if str(timeframe or "").strip().lower() not in {"days", "day", "d"} or normalized_compression != 1:
            print(f"[CBOE] {symbol} supports only Days/1, got {timeframe}/{compression}.")
            return None
        frame = self._frame_for_today(code)
        if frame is None or frame.empty:
            return None
        if code not in self._fallback_notices:
            print(
                f"[CBOE] Fallback active for US.{code}: preceding broker/index sources "
                "may not support this code; using official CBOE daily data. No action is required."
            )
            self._fallback_notices.add(code)
        result = frame
        try:
            if start_date:
                result = result[result.index >= pd.to_datetime(start_date)]
            if end_date:
                result = result[result.index <= pd.to_datetime(end_date)]
        except (TypeError, ValueError):
            return None
        return result.copy()


__all__ = ["CboeDataProvider"]
