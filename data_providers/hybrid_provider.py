"""ThetaData 历史风险字段与 Futu 实时行情的混合数据源。"""

from __future__ import annotations

import math
import re
from zoneinfo import ZoneInfo

import pandas as pd

import config
from common.options.analytics import iv_percentile, parse_option_symbol
from common.options.data_safety import sanitize_market_dataframe
from common.options.universe import is_option_contract_symbol
from live_trader.adapters.futu_symbols import normalize_futu_symbol

from .base_provider import BaseDataProvider
from .futu_provider import FutuDataProvider
from .overlay_provider import OverlayDataProvider
from .thetadata_provider import ThetaDataProvider


class HybridDataProvider(BaseDataProvider):
    """正股历史只用 Futu，不回退 Theta；期权历史与 as_of 链走 ThetaData。实盘正股和期权都叠加 Futu 当前快照。"""

    PRIORITY = 95
    HYBRID_ONLY = True
    HISTORICAL_OPTION_CHAIN = True
    _MARKET_TIMEZONES = {
        "US": "America/New_York",
        "HK": "Asia/Hong_Kong",
        "SH": "Asia/Shanghai",
        "SZ": "Asia/Shanghai",
        "SG": "Asia/Singapore",
        "JP": "Asia/Tokyo",
        "AU": "Australia/Sydney",
    }
    _OPTION_CODE = re.compile(
        r'^(?P<underlying>[A-Z0-9._-]+?)(?P<expiry>\d{6,8})'
        r'(?P<right>[CP])(?P<strike>\d{3,9})$'
    )

    def __init__(self, theta_provider=None, futu_provider=None):
        self.theta_provider = theta_provider or ThetaDataProvider()
        self.futu_provider = futu_provider or FutuDataProvider()
        self.live_mode = False
        self._overlay = OverlayDataProvider(
            self.theta_provider,
            self.futu_provider,
            symbol_mapper=self._provider_symbols,
            merge_realtime=self._merge_live_overlay,
        )

    def set_live_mode(self, enabled: bool) -> None:
        """设置当前 DataManager 是否处于实盘数据路径。"""

        self.live_mode = bool(enabled)
        self._overlay.set_live_mode(enabled)

    def bind_broker(self, broker) -> None:
        """将实盘 Broker 已建立的 Futu 行情会话注入实时子 Provider。"""

        getter = getattr(broker, '_get_quote_context', None)
        if not callable(getter):
            return
        quote_context = getter()
        if quote_context is None:
            return
        old_provider = self.futu_provider
        if getattr(old_provider, '_quote_ctx', None) is quote_context:
            return
        try:
            old_context = getattr(old_provider, '_quote_ctx', None)
            old_owned = bool(getattr(old_provider, '_owns_quote_ctx', False))
            if old_owned and old_context is not None:
                old_context.close()
        except Exception:
            pass
        self.futu_provider = FutuDataProvider(quote_ctx=quote_context)
        self._overlay.bind_realtime_provider(self.futu_provider)

    @staticmethod
    def _finite(value, default=None):
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if math.isfinite(parsed) else default

    @staticmethod
    def _quote_value(row, names):
        """从 Futu 快照行按别名读取第一个非空字段。"""

        if row is None:
            return None
        for name in names:
            if name not in row.index:
                continue
            value = row.get(name)
            if value is None:
                continue
            try:
                if pd.isna(value):
                    continue
            except (TypeError, ValueError):
                pass
            return value
        return None

    def _quote_timestamp(self, row, symbol):
        """解析并按市场时区标准化 Futu 快照时间。"""
        raw_timestamp = self._quote_value(
            row, ("update_time", "timestamp", "time_key", "datetime")
        )
        parsed = pd.to_datetime(raw_timestamp, errors="coerce")
        if pd.isna(parsed):
            return None
        stamp = pd.Timestamp(parsed)
        if stamp.tzinfo is None:
            market = normalize_futu_symbol(symbol).split(".", 1)[0]
            timezone = self._MARKET_TIMEZONES.get(market)
            if timezone:
                try:
                    stamp = stamp.tz_localize(ZoneInfo(timezone))
                except Exception:
                    return None
        return stamp

    def _quote_is_fresh(self, row, symbol) -> bool:
        stamp = self._quote_timestamp(row, symbol)
        if stamp is None:
            return False
        max_age = self._finite(
            getattr(config, "OPTION_RISK_MAX_QUOTE_AGE_SECONDS", 300.0), 300.0
        )
        if max_age is None or max_age < 0:
            max_age = 300.0
        now = pd.Timestamp.now(tz=stamp.tz) if stamp.tzinfo is not None else pd.Timestamp.now()
        age = (now - stamp).total_seconds()
        return math.isfinite(age) and age >= -5.0 and age <= max_age

    @staticmethod
    def _provider_symbols(symbol):
        """为 ThetaData 与 Futu 生成各自接受的期权代码格式。"""

        raw = str(symbol or '').strip().upper()
        code = raw.split('.', 1)[1] if '.' in raw else raw
        match = HybridDataProvider._OPTION_CODE.match(code)
        if match:
            parts = match.groupdict()
            parsed = {
                'underlying': parts['underlying'],
                'expiry': pd.to_datetime(parts['expiry'], format='%y%m%d' if len(parts['expiry']) == 6 else '%Y%m%d', errors='coerce'),
                'strike': int(parts['strike']) / 1000.0,
                'option_type': 'PUT' if parts['right'] == 'P' else 'CALL',
            }
        else:
            parsed = parse_option_symbol(raw)
        if not parsed.get('option_type') or parsed.get('strike') is None:
            normalized = normalize_futu_symbol(raw)
            return normalized or raw, normalized or raw
        expiry = pd.Timestamp(parsed.get('expiry')).strftime('%y%m%d')
        right = 'P' if parsed['option_type'] == 'PUT' else 'C'
        strike_scaled = int(round(float(parsed['strike']) * 1000.0))
        underlying = str(parsed.get('underlying') or '').upper()
        market = raw.split('.', 1)[0] if '.' in raw else 'US'
        theta_symbol = f'US.{underlying}{expiry}{right}{strike_scaled:08d}'
        futu_symbol = normalize_futu_symbol(
            f'{market}.{underlying}{expiry}{right}{strike_scaled}'
        )
        return theta_symbol, futu_symbol

    @staticmethod
    def _daily_index(index):
        """返回与历史索引时区一致的当前交易日午夜。"""

        timezone = getattr(index, "tz", None)
        now = pd.Timestamp.now(tz=timezone) if timezone is not None else pd.Timestamp.now()
        return now.normalize()

    def _merge_live_overlay(self, frame, *, symbol, timeframe, realtime_provider, realtime_symbol):
        """适配通用叠加层的回调签名。"""

        return self._merge_live_quote(
            frame, symbol, timeframe, realtime_symbol, realtime_provider
        )

    def _merge_live_quote(self, frame, symbol, timeframe, futu_symbol, futu_provider=None):
        """把 Futu 当前快照合并到历史末端。"""

        provider = futu_provider or self.futu_provider
        getter = getattr(provider, 'get_market_snapshot', None)
        if not callable(getter):
            return None
        snapshot = getter([futu_symbol])
        if not isinstance(snapshot, pd.DataFrame) or snapshot.empty:
            return None
        rows = snapshot.to_dict("records")
        if len(rows) != 1:
            return None
        quote = snapshot.iloc[0]
        normalized_symbol = normalize_futu_symbol(futu_symbol)
        quote_symbol = normalize_futu_symbol(
            self._quote_value(quote, ("code", "symbol"))
        )
        if not quote_symbol or not normalized_symbol or quote_symbol != normalized_symbol:
            return None
        if not self._quote_is_fresh(quote, symbol):
            return None

        result = frame.copy()
        if not isinstance(result.index, pd.DatetimeIndex):
            result.index = pd.to_datetime(result.index, errors="coerce")
        result = result[~result.index.isna()].sort_index()
        if result.empty:
            return None
        current = result.iloc[-1].copy()

        open_price = self._finite(self._quote_value(quote, ("open_price", "open")))
        high_price = self._finite(self._quote_value(quote, ("high_price", "high")))
        low_price = self._finite(self._quote_value(quote, ("low_price", "low")))
        last_price = self._finite(
            self._quote_value(quote, ("last_price", "last", "price", "close"))
        )
        if any(value is None or value <= 0 for value in (open_price, high_price, low_price, last_price)):
            return None
        if high_price < max(open_price, low_price, last_price) or low_price > min(open_price, high_price, last_price):
            return None
        current.update({
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": last_price,
            "volume": max(0.0, self._finite(self._quote_value(quote, ("volume", "vol")), 0.0)),
        })

        option_fields = (
            ("option_type", ("option_type",)),
            ("strike", ("option_strike_price", "strike_price", "strike")),
            ("expiry", ("strike_time", "option_expiry_date", "expiry")),
            ("bid", ("bid_price", "bid")),
            ("ask", ("ask_price", "ask")),
            ("last", ("last_price", "last", "price", "close")),
            ("open_interest", ("option_open_interest", "open_interest", "oi")),
            ("iv", ("option_implied_volatility", "implied_volatility", "iv")),
            ("delta", ("option_delta", "delta")),
            ("gamma", ("option_gamma", "gamma")),
            ("theta", ("option_theta", "theta")),
            ("vega", ("option_vega", "vega")),
            ("rho", ("option_rho", "rho")),
            ("contract_multiplier", (
                "option_contract_multiplier", "option_contract_size",
                "contract_multiplier", "contract_size",
            )),
            ("currency", ("currency", "quote_currency", "currency_code")),
        )
        quote_option_values = {}
        for output_name, aliases in option_fields:
            value = self._quote_value(quote, aliases)
            if value is not None:
                quote_option_values[output_name] = value

        is_option = bool(parse_option_symbol(symbol).get('option_type'))
        if is_option:
            # 当前行只允许携带本次 Futu 快照确认的期权风险字段，先清空
            # ThetaData 末行中的旧盘口/Greeks，避免缺字段时隐式沿用旧事实。
            for output_name, _aliases in option_fields:
                current[output_name] = pd.NA
            required_option_fields = (
                'option_type', 'strike', 'expiry', 'bid', 'ask', 'last',
                'open_interest', 'iv', 'delta', 'contract_multiplier',
            )
            if any(name not in quote_option_values for name in required_option_fields):
                # 不能把昨日 ThetaData 的风险字段复制到当前盘口行。
                return None
            option_type = str(quote_option_values.get('option_type') or '').strip().upper()
            if option_type not in {'P', 'PUT', 'C', 'CALL'}:
                return None
            try:
                strike = float(quote_option_values['strike'])
                expiry = pd.to_datetime(quote_option_values['expiry'], errors='coerce')
                bid = float(quote_option_values['bid'])
                ask = float(quote_option_values['ask'])
                last = float(quote_option_values['last'])
                open_interest = float(quote_option_values['open_interest'])
                iv = float(quote_option_values['iv'])
                delta = float(quote_option_values['delta'])
                multiplier = float(quote_option_values['contract_multiplier'])
            except (TypeError, ValueError, OverflowError):
                return None
            if (
                pd.isna(expiry)
                or not all(math.isfinite(value) for value in
                            (strike, bid, ask, last, open_interest, iv, delta, multiplier))
                or strike <= 0 or bid < 0 or ask < bid or last <= 0
                or open_interest < 0 or iv < 0 or multiplier <= 1.0
            ):
                return None
            parsed_contract = parse_option_symbol(symbol)
            expected_type = str(parsed_contract.get('option_type') or '').upper()
            if expected_type and option_type not in {expected_type, expected_type[:1]}:
                return None
            expected_strike = self._finite(parsed_contract.get('strike'))
            if expected_strike is not None and abs(strike - expected_strike) > max(
                1e-9, abs(expected_strike) * 1e-9
            ):
                return None
            expected_expiry = pd.Timestamp(parsed_contract.get('expiry'))
            if pd.isna(expected_expiry) or pd.Timestamp(expiry).normalize() != expected_expiry.normalize():
                return None
        for key, value in quote_option_values.items():
            current[key] = value

        if "iv" in current:
            iv_value = self._finite(current.get("iv"))
            if iv_value is not None:
                current["iv"] = iv_value / 100.0 if iv_value > 1.0 else iv_value

        is_daily = str(timeframe or "").strip().lower() in {"days", "day", "d"}
        quote_timestamp = self._quote_timestamp(quote, symbol)
        if quote_timestamp is None:
            return None
        target_index = quote_timestamp
        index_tz = getattr(result.index, 'tz', None)
        if index_tz is None and target_index.tzinfo is not None:
            target_index = target_index.tz_localize(None)
        elif index_tz is not None and target_index.tzinfo is None:
            target_index = target_index.tz_localize(index_tz)
        elif index_tz is not None:
            target_index = target_index.tz_convert(index_tz)
        if is_daily:
            target_index = target_index.normalize()
        for column in current.index:
            if column not in result.columns:
                result[column] = pd.NA
        result.loc[target_index] = current
        result = result[~result.index.duplicated(keep="last")].sort_index()

        if is_option and "iv" in result.columns and "option_type" in result.columns:
            iv_series = pd.to_numeric(result["iv"], errors="coerce")
            result["iv_percentile"] = iv_percentile(
                result, iv_series, window=252, explicit=False
            )
        return sanitize_market_dataframe(result, require_ohlcv=True)

    def get_option_chain(self, underlying, start=None, end=None, as_of=None,
                           normalized=True, **kwargs):
        """回测走 Theta 历史链；实盘当前链走 Futu，不回退到历史末行。"""
        if self.live_mode and as_of is None:
            futu = self.futu_provider
            method = None
            if normalized:
                method = getattr(futu, "get_option_chain_normalized", None)
            if not callable(method):
                method = getattr(futu, "get_option_chain", None)
            if not callable(method):
                return None
            try:
                return method(
                    underlying,
                    start=start,
                    end=end,
                    normalized=normalized,
                    timestamp=kwargs.get("timestamp"),
                    as_of=as_of,
                )
            except TypeError:
                try:
                    return method(underlying, start=start, end=end)
                except TypeError:
                    return method(underlying)
            except Exception:
                return None
        theta = self.theta_provider
        getter = getattr(theta, "get_option_chain", None)
        if not callable(getter):
            return None
        return getter(
            underlying,
            start=start,
            end=end,
            as_of=as_of,
            normalized=normalized,
            **kwargs,
        )

    def take_empty_result_count(self):
        """转发 Theta 空结果计数；正股 Futu 路径没有该计数。"""
        getter = getattr(self.theta_provider, "take_empty_result_count", None)
        if not callable(getter):
            return 0
        try:
            return int(getter() or 0)
        except Exception:
            return 0

    def take_retryable_fail_count(self):
        """转发 Theta 超时/瞬时失败计数。"""
        getter = getattr(self.theta_provider, "take_retryable_fail_count", None)
        if not callable(getter):
            return 0
        try:
            return int(getter() or 0)
        except Exception:
            return 0

    def get_data(self, symbol: str, start_date=None, end_date=None,
                 timeframe: str = "Days", compression: int = 1,
                 refresh: bool = False) -> pd.DataFrame:
        """期权走 Theta 历史；正股走 Futu 历史且不回退 Theta。实盘再叠加 Futu 当前快照。"""

        if is_option_contract_symbol(symbol):
            return self._overlay.get_data(
                symbol, start_date, end_date, timeframe, compression, refresh=refresh
            )
        getter = getattr(self.futu_provider, "get_data", None)
        frame = None
        if callable(getter):
            try:
                frame = getter(symbol, start_date, end_date, timeframe, compression)
            except Exception:
                frame = None
        if frame is None or getattr(frame, "empty", False):
            # theta+futu 的正股历史必须来自 Futu/OpenD；Theta 股票历史需要更高订阅，
            # 不能在 OpenD 断连时静默改换数据源。
            return None
        if not self.live_mode:
            return frame
        _historical_symbol, futu_symbol = self._provider_symbols(symbol)
        merged = self._merge_live_overlay(
            frame,
            symbol=symbol,
            timeframe=timeframe,
            realtime_provider=self.futu_provider,
            realtime_symbol=futu_symbol,
        )
        if merged is None or getattr(merged, "empty", False):
            return frame
        return merged

    def close(self):
        """关闭混合层内部创建的 Provider 连接。"""

        self._overlay.close()


__all__ = ["HybridDataProvider"]
