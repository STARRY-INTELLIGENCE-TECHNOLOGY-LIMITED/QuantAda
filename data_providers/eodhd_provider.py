"""EOD Historical Data 的股票日线 Provider。"""

from __future__ import annotations

import time
import os
import sys
from urllib.parse import parse_qsl

import pandas as pd
import requests

import config
from common.live_runtime import dependency_install_hint
from common.options.analytics import parse_option_symbol
from common.options.data_safety import sanitize_market_dataframe
from common.options.events import normalize_dividend_calendar
from .base_provider import BaseDataProvider, REQUEST_ATTEMPTS


_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 20.0
_RETRY_BACKOFF_SECONDS = 0.25
_US_SUFFIX = ".US"

_TRACEBACKLIMIT_MISSING = object()
_previous_tracebacklimit = getattr(sys, "tracebacklimit", _TRACEBACKLIMIT_MISSING)
try:
    from eodhd import APIClient as _EODHDAPIClient
    _EODHD_IMPORT_ERROR = None
except Exception as exc:
    _EODHDAPIClient = None
    _EODHD_IMPORT_ERROR = exc
    print(dependency_install_hint("eodhd", exc))
finally:
    # SDK 1.x 在导入时修改全局 traceback 输出限制，不能把该副作用泄漏给框架。
    if _previous_tracebacklimit is _TRACEBACKLIMIT_MISSING:
        sys.__dict__.pop("tracebacklimit", None)
    else:
        sys.tracebacklimit = _previous_tracebacklimit


class EODHDRequestError(RuntimeError):
    """不包含认证参数的 EODHD 请求错误。"""

    def __init__(self, status_code, message=""):
        self.status_code = status_code
        safe_message = str(message or "").replace("\n", " ").strip()[:200]
        super().__init__(f"HTTP {status_code}: {safe_message}" if status_code else safe_message)


def _retryable_status(status_code):
    """只有超时、限流和 5xx 才重试；HTTP 200 的业务警告与 4xx 立即失败。"""
    return status_code is None or status_code in {408, 425, 429} or (
        isinstance(status_code, int) and status_code >= 500
    )


if _EODHDAPIClient is not None:
    class _BoundedEODHDClient(_EODHDAPIClient):
        """使用有界超时并通过 requests 默认代理环境访问 EODHD。"""

        def _rest_get(self, endpoint="", uri="", querystring=""):
            if not str(endpoint or "").strip():
                raise ValueError("EODHD endpoint is empty")
            url = f"{self._api_url}/{str(endpoint).strip('/')}/{str(uri).strip('/')}"
            params = {"api_token": self._api_key, "fmt": "json"}
            params.update(dict(parse_qsl(str(querystring or "").lstrip("&"), keep_blank_values=True)))
            try:
                response = requests.get(
                    url,
                    params=params,
                    timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
                )
            except requests.RequestException as exc:
                raise EODHDRequestError(None, type(exc).__name__) from None
            try:
                payload = response.json()
            except ValueError as exc:
                raise EODHDRequestError(response.status_code, "invalid JSON response") from exc
            if response.status_code != 200:
                message = ""
                if isinstance(payload, dict):
                    message = payload.get("message") or payload.get("error") or payload.get("warning") or ""
                raise EODHDRequestError(response.status_code, message)
            return self._frame_from_payload(payload, response.status_code)

        @staticmethod
        def _frame_from_payload(payload, status_code):
            """把 EODHD JSON 收成行情表；warning/error 对象直接失败，不当成一行数据。"""
            if isinstance(payload, list):
                if not payload:
                    return pd.DataFrame()
                if all(isinstance(item, dict) for item in payload):
                    frame = pd.DataFrame(payload)
                else:
                    raise EODHDRequestError(status_code, "unsupported JSON response")
            elif isinstance(payload, dict):
                if payload and all(isinstance(value, list) for value in payload.values()):
                    frame = pd.DataFrame(payload)
                else:
                    frame = pd.DataFrame([payload])
            else:
                raise EODHDRequestError(status_code, "unsupported JSON response")
            columns = {str(column).strip().lower() for column in frame.columns}
            if "error" in columns or "warning" in columns:
                message_column = next(
                    column for column in frame.columns
                    if str(column).strip().lower() in {"error", "warning", "message"}
                )
                message = str(frame.iloc[0][message_column]) if not frame.empty else "EODHD warning"
                raise EODHDRequestError(status_code, message)
            return frame
else:
    _BoundedEODHDClient = None


class EODHDDataProvider(BaseDataProvider):
    """读取 EODHD 美股、ETF 的未复权日线并标准化为 OHLCV。"""

    PRIORITY = 85

    def __init__(self, api_token=None, client=None):
        token = api_token
        if token is None:
            token = str(
                os.environ.get("EODHD_API_TOKEN")
                or getattr(config, "EODHD_API_TOKEN", "")
                or ""
            ).strip()
        self.token = token
        self.client = client
        if self.client is None and self.token and _BoundedEODHDClient is not None:
            try:
                self.client = _BoundedEODHDClient(self.token)
            except Exception as exc:
                print(f"[EODHD] Client initialization failed: {type(exc).__name__}")
        if not self.token:
            print("[EODHD] EODHD_API_TOKEN is not configured; EODHD source will return no data.")
        elif self.client is None and _BoundedEODHDClient is None:
            print(dependency_install_hint("eodhd", _EODHD_IMPORT_ERROR))

    @staticmethod
    def supports_symbol(symbol):
        """只接受美股代码，不吞掉期权代码或其它市场代码。"""
        text = str(symbol or "").strip().upper()
        if not text or parse_option_symbol(text).get("option_type"):
            return False
        if "." not in text:
            return bool(text)
        market, _code = text.split(".", 1)
        return market == "US"

    @staticmethod
    def _eodhd_symbol(symbol):
        """转换为 EODHD 的 TICKER.US 格式。"""
        text = str(symbol or "").strip().upper()
        if "." in text:
            market, ticker = text.split(".", 1)
            if market != "US":
                return None
        else:
            ticker = text
        ticker = ticker.strip().replace(".", "-")
        if not ticker or any(char.isspace() for char in ticker):
            return None
        return f"{ticker}{_US_SUFFIX}"

    @staticmethod
    def _date_text(value):
        """兼容 YYYYMMDD 与标准日期字符串。"""
        if value is None or not str(value).strip():
            return ""
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("America/New_York").tz_localize(None)
        return stamp.strftime("%Y-%m-%d")

    @staticmethod
    def _period(timeframe, compression):
        """解析 EODHD 日、周、月原生周期。"""
        try:
            step = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            return None
        if step != 1:
            return None
        value = str(timeframe or "Days").strip().lower()
        return {"days": "d", "day": "d", "d": "d", "weeks": "w", "week": "w", "w": "w", "months": "m", "month": "m", "m": "m"}.get(value)

    def get_data(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
        timeframe: str = "Days",
        compression: int = 1,
    ) -> pd.DataFrame | None:
        """获取有限请求窗口的美股 OHLCV，并保留未复权 close。"""
        if not self.supports_symbol(symbol) or self.client is None:
            return None
        period = self._period(timeframe, compression)
        if period is None:
            return None
        start = self._date_text(start_date)
        end = self._date_text(end_date)
        if start is None or end is None:
            return None
        if start and end and pd.Timestamp(end) < pd.Timestamp(start):
            return None
        ticker = self._eodhd_symbol(symbol)
        if ticker is None:
            return None

        frame = None
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                frame = self.client.get_historical_data(
                    ticker,
                    interval=period,
                    iso8601_start=start,
                    iso8601_end=end,
                )
                break
            except EODHDRequestError as exc:
                if not _retryable_status(exc.status_code) or attempt >= REQUEST_ATTEMPTS:
                    print(f"[EODHD] {ticker} history failed: {exc}")
                    return None
                print(f"[EODHD] {ticker} history retry {attempt + 1}/{REQUEST_ATTEMPTS} after HTTP {exc.status_code}")
            except (KeyError, TypeError, ValueError) as exc:
                print(f"[EODHD] {ticker} history failed: {type(exc).__name__}: {exc}")
                return None
            time.sleep(min(_RETRY_BACKOFF_SECONDS * attempt, 1.0))

        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return None
        result = frame.copy()
        if "date" in result.columns:
            result.index = pd.to_datetime(result.pop("date"), errors="coerce")
        elif "datetime" in result.columns:
            result.index = pd.to_datetime(result.pop("datetime"), errors="coerce")
        else:
            result.index = pd.to_datetime(result.index, errors="coerce")
        if getattr(result.index, "tz", None) is not None:
            result.index = result.index.tz_convert("America/New_York").tz_localize(None)
        result.index = pd.DatetimeIndex(result.index).normalize()
        result.index.name = "datetime"
        result.columns = [str(column).strip().lower() for column in result.columns]
        required = ["open", "high", "low", "close", "volume"]
        if any(column not in result.columns for column in required):
            print(f"[EODHD] {ticker} response is missing OHLCV columns.")
            return None
        result = result.loc[:, required]
        for column in required:
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result = result.loc[result.index.notna()]
        if start:
            result = result.loc[result.index >= pd.Timestamp(start)]
        if end:
            result = result.loc[result.index <= pd.Timestamp(end)]
        result = result[~result.index.duplicated(keep="last")].sort_index()
        if result.empty:
            return None
        result.attrs["eodhd_symbol"] = ticker
        result.attrs["price_adjustment"] = "unadjusted_close"
        result = sanitize_market_dataframe(result, require_ohlcv=True)
        return result if isinstance(result, pd.DataFrame) and not result.empty else None

    def get_dividend_calendar(
        self, symbols=None, start_date=None, end_date=None, as_of=None, deadline=None,
    ):
        """读取带声明日的 EODHD 分红历史，不把缺失响应解释成空日历。"""
        if self.client is None:
            return None
        raw_symbols = [symbols] if isinstance(symbols, str) else list(symbols or [])
        tickers = []
        for symbol in raw_symbols:
            if not self.supports_symbol(symbol):
                continue
            ticker = self._eodhd_symbol(symbol)
            if ticker:
                tickers.append((str(symbol).strip().upper(), ticker))
        tickers = list(dict.fromkeys(tickers))
        if not tickers:
            return None
        start = self._date_text(start_date)
        end = self._date_text(end_date)
        if start is None or end is None:
            return None

        parts = []
        for symbol, ticker in tickers:
            if deadline is not None and time.monotonic() >= float(deadline) - _READ_TIMEOUT_SECONDS:
                return None
            response = None
            for attempt in range(1, REQUEST_ATTEMPTS + 1):
                query = ""
                if start:
                    query += f"&from={start}"
                if end:
                    query += f"&to={end}"
                try:
                    getter = getattr(self.client, "_rest_get", None)
                    if callable(getter):
                        response = getter("div", ticker, query)
                    else:
                        response = self.client.get_historical_dividends_data(
                            ticker=ticker,
                            date_from=start or None,
                            date_to=end or None,
                        )
                    break
                except EODHDRequestError as exc:
                    if not _retryable_status(exc.status_code) or attempt >= REQUEST_ATTEMPTS:
                        print(f"[EODHD] {ticker} dividend history failed: {exc}")
                        return None
                    print(f"[EODHD] {ticker} dividend retry {attempt + 1}/{REQUEST_ATTEMPTS} after HTTP {exc.status_code}")
                except (KeyError, TypeError, ValueError) as exc:
                    print(f"[EODHD] {ticker} dividend history failed: {type(exc).__name__}: {exc}")
                    return None
                time.sleep(min(_RETRY_BACKOFF_SECONDS * attempt, 1.0))

            if isinstance(response, pd.DataFrame):
                rows = response.copy()
            elif isinstance(response, list):
                rows = pd.DataFrame(response)
            elif response is None:
                return None
            else:
                print(f"[EODHD] {ticker} dividend response is not a table.")
                return None
            if rows.empty:
                continue
            columns = {str(column).strip().lower(): column for column in rows.columns}
            ex_column = next((columns[name] for name in ("date", "ex_date", "ex-dividend-date") if name in columns), None)
            announcement_column = next((columns[name] for name in ("declarationdate", "declaration_date", "announcement_date") if name in columns), None)
            amount_column = next((columns[name] for name in ("unadjustedvalue", "unadjusted_value", "value", "cash_per_share") if name in columns), None)
            currency_column = next((columns[name] for name in ("currency", "currency_code") if name in columns), None)
            if ex_column is None or announcement_column is None or amount_column is None or currency_column is None:
                print(f"[EODHD] {ticker} dividend response is missing PIT fields.")
                return None
            part = pd.DataFrame({
                "symbol": symbol,
                "ex_date": rows[ex_column],
                "announcement_date": rows[announcement_column],
                "cash_per_share": rows[amount_column],
                "currency": rows[currency_column],
            })
            parts.append(part)

        raw = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
            columns=["symbol", "ex_date", "announcement_date", "cash_per_share", "currency"]
        )
        result = normalize_dividend_calendar(
            raw,
            symbols=[symbol for symbol, _ticker in tickers],
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
        )
        if result is None:
            return None
        fields_complete = result.empty or (
            result["announcement_date"].notna().all()
            and pd.to_numeric(result["cash_per_share"], errors="coerce").map(
                lambda value: pd.notna(value) and float(value) > 0
            ).all()
            and result["currency"].astype("string").fillna("").str.strip().ne("").all()
        )
        result.attrs["historical_as_of_supported"] = bool(fields_complete)
        result.attrs["current_snapshot_supported"] = bool(fields_complete)
        result.attrs["as_of_supported"] = bool(fields_complete)
        result.attrs["source"] = "eodhd_historical_dividends"
        return result


__all__ = ["EODHDDataProvider", "EODHDRequestError"]
