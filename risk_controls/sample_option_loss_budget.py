"""按可承受亏损平掉期权仓位。

股票百分比止损只比较价格方向，不能衡量权利金、乘数和结构最大亏损。
返回 ``SELL`` 时，引擎把该 feed 的已确认仓位打回零：多头卖平，空头买平。

百分比预算是已定义最大亏损的比例。多头的上限是已付权利金，纵向价差的上限是
宽度减净权利金，短 Put 的上限是执行价减权利金。裸短 Call 没有定义上限，
必须另给 ``max_loss_cash``。备兑短 Call 的期权市值上涨会被正股抵消，不在这里单腿止损。
账户还能亏多少，继续用 ``sample_max_drawdown_kill_switch``。

挂载:
python run.py strategies.options.sample_long_option_strategy.SampleLongPutStrategy ^
  --symbols US.AAPL --risk risk_controls.sample_option_loss_budget ^
  --risk_params "{'max_loss_pct': 0.5, 'max_loss_cash': 500}"
"""

from __future__ import annotations

import math

import pandas as pd

from common.data_view import position_price, position_size
from common.options.analytics import parse_option_symbol, underlying_key
from common.options.loss_budget import OptionLegSnapshot, assess_symbol_loss
from risk_controls.base_risk_control import BaseRiskControl


class SampleOptionLossBudget(BaseRiskControl):
    """期权可承受亏损风控。数据不足时不触发。"""

    params = {
        "max_loss_pct": 0.50,
        "max_loss_cash": None,
    }

    def __init__(self, broker, params=None):
        super().__init__(broker, params)
        self.exit_triggered = set()
        self._logged = set()
        self._log(
            "SampleOptionLossBudget initialized with "
            f"max_loss_pct={self.p.max_loss_pct}, max_loss_cash={self.p.max_loss_cash}"
        )

    def check(self, data) -> str:
        symbol = str(getattr(data, "_name", "") or "")
        if not symbol or symbol in self.exit_triggered:
            return None
        meta = _contract_meta(data)
        if meta is None:
            return None
        size = position_size(self.broker, data)
        if size == 0:
            return None

        held = self._held_records()
        if self._partner_unpriced(symbol, meta, size, held):
            self._log_once(
                ("unpriced", symbol),
                f"[OptionLossBudget] {symbol} hedge is not fully priced; skip exit.",
            )
            return None

        snapshots = [item for item in held if item["snapshot"] is not None]
        legs = [item["snapshot"] for item in snapshots]
        current = self._snapshot_from(data, meta, size)
        if current is None:
            current = next((leg for leg in legs if leg.symbol == symbol), None)
        if current is None:
            self._log_once(
                ("unpriced", symbol),
                f"[OptionLossBudget] {symbol} premium, mark or multiplier is unavailable; skip exit.",
            )
            return None
        legs = [current if leg.symbol == symbol else leg for leg in legs]
        if not any(leg.symbol == symbol for leg in legs):
            legs.append(current)

        decision = assess_symbol_loss(
            symbol,
            legs,
            max_loss_pct=self.p.max_loss_pct,
            max_loss_cash=self.p.max_loss_cash,
            covered=self._short_calls_covered(meta, legs),
        )
        if decision.reason in {"undefined_risk", "covered_call", "incomplete_hedge", "no_budget"}:
            self._log_once(
                (decision.reason, symbol),
                f"[OptionLossBudget] {symbol} not percent-managed: {decision.reason}.",
            )
        if not decision.breached or symbol not in decision.symbols:
            return None

        self.exit_triggered.add(symbol)
        self._log(
            "[OptionLossBudget] loss budget breached for "
            f"{symbol}. loss={decision.loss}, budget={decision.budget}, "
            f"defined_max_loss={decision.defined_max_loss}."
        )
        return "SELL"


    def _snapshot_from(self, feed, meta, size):
        entry = position_price(self.broker, feed)
        mark = _mark(feed, self.broker)
        multiplier = self._multiplier(feed)
        if entry <= 0 or mark is None or multiplier <= 0:
            return None
        return OptionLegSnapshot(
            symbol=str(getattr(feed, "_name", "") or ""),
            option_type=meta["option_type"],
            strike=meta["strike"],
            expiry=meta["expiry"],
            underlying=meta["underlying"],
            signed_quantity=size,
            entry_premium=entry,
            mark_premium=mark,
            multiplier=multiplier,
        )

    def _held_records(self):
        records = []
        seen = set()
        for feed in list(getattr(self.broker, "datas", []) or []):
            record = self._record(feed)
            if record is None or record["symbol"] in seen:
                continue
            seen.add(record["symbol"])
            records.append(record)
        return records

    def _record(self, feed):
        symbol = str(getattr(feed, "_name", "") or "")
        meta = _contract_meta(feed)
        size = position_size(self.broker, feed)
        if not symbol or meta is None or size == 0:
            return None
        entry = position_price(self.broker, feed)
        mark = _mark(feed, self.broker)
        multiplier = self._multiplier(feed)
        snapshot = None
        if entry > 0 and mark is not None and multiplier > 0:
            snapshot = OptionLegSnapshot(
                symbol=symbol,
                option_type=meta["option_type"],
                strike=meta["strike"],
                expiry=meta["expiry"],
                underlying=meta["underlying"],
                signed_quantity=size,
                entry_premium=entry,
                mark_premium=mark,
                multiplier=multiplier,
            )
        return {
            "symbol": symbol,
            "meta": meta,
            "size": size,
            "snapshot": snapshot,
        }

    def _partner_unpriced(self, symbol, meta, size, held):
        for record in held:
            other = record["meta"]
            if record["symbol"] == symbol or record["snapshot"] is not None:
                continue
            if not _same_family(meta, other):
                continue
            if size * record["size"] < 0:
                return True
        return False

    def _short_calls_covered(self, meta, legs):
        identity = underlying_key(meta["underlying"])
        required = 0.0
        for leg in legs:
            if leg.signed_quantity >= 0 or leg.option_type != "CALL":
                continue
            if underlying_key(leg.underlying) != identity:
                continue
            if _has_equal_call_hedge(leg, legs):
                continue
            required += abs(leg.signed_quantity) * leg.multiplier
        if required <= 0:
            return False
        shares = 0.0
        found = False
        for feed in list(getattr(self.broker, "datas", []) or []):
            name = str(getattr(feed, "_name", "") or "")
            if parse_option_symbol(name).get("option_type"):
                continue
            if underlying_key(name) != identity:
                continue
            found = True
            shares += position_size(self.broker, feed)
        return found and shares + 1e-9 >= required

    def _multiplier(self, data):
        value = _read_multiplier(self.broker, data)
        if value > 0:
            return value
        symbol = getattr(data, "_name", None)
        for feed in list(getattr(self.broker, "datas", []) or []):
            if feed is data or getattr(feed, "_name", None) != symbol:
                continue
            value = _read_multiplier(self.broker, feed)
            if value > 0:
                return value
        return 0.0

    def _log(self, message):
        logger = getattr(self.broker, "log", None)
        if callable(logger):
            logger(message)
            return
        print(message)

    def _log_once(self, key, message):
        if key in self._logged:
            return
        self._logged.add(key)
        self._log(message)


def _read_multiplier(broker, data):
    getter = getattr(broker, "get_contract_multiplier", None)
    if not callable(getter):
        return 0.0
    try:
        value = float(getter(data))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _mark(data, broker):
    close = getattr(data, "close", None)
    if close is not None:
        try:
            value = float(close[0])
        except (TypeError, ValueError, OverflowError, IndexError):
            value = None
        if value is not None and math.isfinite(value) and value >= 0:
            return value
    if not getattr(broker, "is_live", False):
        return None
    frame = getattr(getattr(data, "p", None), "dataname", None)
    if isinstance(frame, pd.DataFrame) and "close" in frame.columns and not frame.empty:
        values = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if not values.empty:
            value = float(values.iloc[-1])
            if math.isfinite(value) and value >= 0:
                return value
    return None


def _contract_meta(data):
    parsed = parse_option_symbol(getattr(data, "_name", ""))
    option_type = str(parsed.get("option_type") or "").upper()
    strike = parsed.get("strike")
    expiry = parsed.get("expiry")
    underlying = str(parsed.get("underlying") or "")
    row = _metadata_row(data)
    if row is not None:
        raw_type = _row_text(row, ("option_type", "right", "cp", "put_call"))
        if raw_type in {"P", "PUT"}:
            option_type = "PUT"
        elif raw_type in {"C", "CALL"}:
            option_type = "CALL"
        raw_strike = _row_number(row, ("strike", "strike_price", "exercise_price"))
        if raw_strike is not None:
            strike = raw_strike
        raw_expiry = _row_text(row, ("expiry", "expiration", "expiration_date", "expiry_date"))
        if raw_expiry:
            expiry = raw_expiry
        raw_underlying = _row_text(row, ("underlying", "underlying_symbol"))
        if raw_underlying:
            underlying = raw_underlying
    if option_type not in {"PUT", "CALL"}:
        return None
    try:
        strike = float(strike)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(strike) or strike <= 0:
        return None
    expiry_key = ""
    if expiry is not None:
        try:
            stamp = pd.Timestamp(expiry)
            if not pd.isna(stamp):
                expiry_key = stamp.strftime("%Y%m%d")
        except Exception:
            expiry_key = ""
    return {
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry_key,
        "underlying": underlying or underlying_key(getattr(data, "_name", "")),
    }


def _metadata_row(data):
    bar = getattr(data, "_bar", None)
    if bar is not None and hasattr(bar, "get"):
        return bar
    frame = getattr(getattr(data, "p", None), "dataname", None)
    if isinstance(frame, pd.DataFrame) and not frame.empty:
        return frame.iloc[-1]
    return None


def _row_text(row, names):
    for name in names:
        try:
            value = row.get(name)
        except Exception:
            continue
        text = str(value or "").strip().upper()
        if text and text not in {"NAN", "NONE", "NAT"}:
            return text
    return ""


def _row_number(row, names):
    for name in names:
        try:
            value = row.get(name)
        except Exception:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _same_family(left, right):
    return (
        left["option_type"] == right["option_type"]
        and left["expiry"]
        and left["expiry"] == right["expiry"]
        and underlying_key(left["underlying"]) == underlying_key(right["underlying"])
    )


def _has_equal_call_hedge(leg, legs):
    for other in legs:
        if other.symbol == leg.symbol or other.option_type != "CALL" or other.signed_quantity <= 0:
            continue
        if other.expiry != leg.expiry or not leg.expiry:
            continue
        if underlying_key(other.underlying) != underlying_key(leg.underlying):
            continue
        if abs(abs(other.signed_quantity) - abs(leg.signed_quantity)) > 1e-9:
            continue
        if abs(other.multiplier - leg.multiplier) > 1e-9:
            continue
        if abs(other.strike - leg.strike) > 1e-9:
            return True
    return False
