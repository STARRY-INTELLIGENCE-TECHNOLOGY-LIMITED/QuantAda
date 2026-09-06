"""通用期权链到 QuantAda 统一模型的纯转换工具。"""

from __future__ import annotations

import re
import math

import pandas as pd


OPTION_CHAIN_COLUMNS = (
    "timestamp",
    "underlying",
    "spot",
    "option_symbol",
    "option_type",
    "strike",
    "expiry",
    "bid",
    "ask",
    "last",
    "volume",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "contract_multiplier",
    "currency",
)

# 合约发现和可交易报价使用同一套列名，但两者的完整性要求不同。
# 交易路径默认要求以下字段都能由同一时刻的行情快照确认，避免把 NaN
# 当作可成交报价或风险参数；仅做合约发现时可传入 ``require_quotes=False``。
CONTRACT_REQUIRED_COLUMNS = (
    "option_symbol", "option_type", "strike", "expiry", "contract_multiplier",
)
QUOTE_REQUIRED_COLUMNS = (
    "spot", "bid", "ask", "last", "volume", "open_interest", "iv",
    "delta", "gamma", "theta", "vega", "rho", "currency",
)

_ALIASES = {
    "option_symbol": ("option_symbol", "code", "symbol", "option_code"),
    "option_type": ("option_type", "call_put", "cp", "type"),
    "strike": ("strike", "strike_price", "exercise_price"),
    "expiry": ("expiry", "expiration", "strike_time", "expiry_date"),
    "spot": ("spot", "spot_price", "underlying_price", "stock_price"),
    "bid": ("bid", "bid_price", "bidPrice"),
    "ask": ("ask", "ask_price", "askPrice"),
    "last": ("last", "last_price", "price", "close"),
    "volume": ("volume", "vol", "volume_total"),
    "open_interest": ("open_interest", "open_interest_qty", "oi"),
    "iv": ("iv", "implied_volatility", "implied_vol", "impliedVolatility"),
    "delta": ("delta",),
    "gamma": ("gamma",),
    "theta": ("theta",),
    "vega": ("vega",),
    "rho": ("rho",),
    "contract_multiplier": (
        "contract_multiplier",
        "contract_size",
        "option_contract_multiplier",
        "option_contract_size",
    ),
    "currency": ("currency", "quote_currency", "currency_code"),
    "timestamp": ("timestamp", "time", "time_key", "update_time", "datetime"),
}

_OPTION_TYPE_RE = re.compile(r"\d{6,8}([CP])\d+$", re.IGNORECASE)


def _column_value(row, names):
    """按大小写不敏感别名读取一行字段。"""
    for name in names:
        if name in row.index:
            return row[name]
        lowered = name.lower()
        for column in row.index:
            if str(column).lower() == lowered:
                return row[column]
    return None


def _finite_number(value):
    """转换为有限浮点数；缺失值返回 NaN。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return float("nan")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    return parsed if pd.notna(parsed) and abs(parsed) != float("inf") else float("nan")


def _normalise_option_type(value, symbol):
    """标准化 CALL/PUT；无法确认时返回空值。"""
    text = str(value or "").strip().upper()
    if text.endswith("CALL") or text in {"C", "CALL", "1"}:
        return "CALL"
    if text.endswith("PUT") or text in {"P", "PUT", "2"}:
        return "PUT"
    match = _OPTION_TYPE_RE.search(str(symbol or ""))
    if match:
        return "CALL" if match.group(1).upper() == "C" else "PUT"
    return None


def normalize_option_chain(
    raw_chain: pd.DataFrame,
    underlying: str,
    *,
    timestamp=None,
    as_of=None,
    contract_multiplier=None,
    symbol_normalizer=None,
    require_quotes=True,
) -> pd.DataFrame | None:
    """严格转换期权链，缺失关键事实、重复合约或过期数据时返回 None。

    ``as_of`` 用于历史回放的可见性边界；不传入时不会用当前时间误判历史链。
    合约乘数只接受链数据、显式元数据或调用方传入值，不使用默认乘数 1。
    """
    if not isinstance(raw_chain, pd.DataFrame) or raw_chain.empty:
        return None
    normalizer = symbol_normalizer or (lambda value: str(value or "").strip().upper())
    normalized_underlying = normalizer(underlying)
    if not normalized_underlying:
        return None

    query_timestamp = pd.Timestamp(timestamp) if timestamp is not None else pd.Timestamp.now(tz="UTC")
    if query_timestamp.tzinfo is None:
        query_timestamp = query_timestamp.tz_localize("UTC")
    else:
        query_timestamp = query_timestamp.tz_convert("UTC")
    visibility_time = None
    if as_of is not None:
        visibility_time = pd.Timestamp(as_of)
        if visibility_time.tzinfo is None:
            visibility_time = visibility_time.tz_localize("UTC")
        else:
            visibility_time = visibility_time.tz_convert("UTC")

    rows = []
    missing_quote_fields = set()
    used_caller_timestamp = False
    for _, source in raw_chain.iterrows():
        option_symbol = normalizer(
            _column_value(source, _ALIASES["option_symbol"])
        )
        option_type = _normalise_option_type(
            _column_value(source, _ALIASES["option_type"]), option_symbol
        )
        strike = _finite_number(_column_value(source, _ALIASES["strike"]))
        expiry = pd.to_datetime(
            _column_value(source, _ALIASES["expiry"]), errors="coerce", utc=True
        )
        row_timestamp = _column_value(source, _ALIASES["timestamp"])
        row_timestamp = pd.to_datetime(row_timestamp, errors="coerce", utc=True)
        timestamp_source = "source"
        if pd.isna(row_timestamp):
            # 历史回放必须有源数据时间戳，不能把当前链伪装成历史链。
            # 实时调用允许调用方明确提供抓取时间作为快照边界，并在 attrs 中留痕。
            if timestamp is None:
                return None
            row_timestamp = query_timestamp
            timestamp_source = "caller"
            used_caller_timestamp = True
        if visibility_time is not None and row_timestamp > visibility_time:
            return None
        multiplier = _finite_number(
            _column_value(source, _ALIASES["contract_multiplier"])
        )
        if pd.isna(multiplier) and contract_multiplier is not None:
            multiplier = _finite_number(contract_multiplier)
        if (
            not option_symbol
            or option_type is None
            or pd.isna(strike)
            or strike <= 0
            or pd.isna(expiry)
            or pd.isna(multiplier)
            or multiplier <= 0
        ):
            return None
        if visibility_time is not None and expiry.normalize() < visibility_time.normalize():
            return None
        row = {
            "timestamp": row_timestamp,
            "underlying": normalized_underlying,
            "spot": _finite_number(_column_value(source, _ALIASES["spot"])),
            "option_symbol": option_symbol,
            "option_type": option_type,
            "strike": strike,
            "expiry": expiry,
            "bid": _finite_number(_column_value(source, _ALIASES["bid"])),
            "ask": _finite_number(_column_value(source, _ALIASES["ask"])),
            "last": _finite_number(_column_value(source, _ALIASES["last"])),
            "volume": _finite_number(_column_value(source, _ALIASES["volume"])),
            "open_interest": _finite_number(
                _column_value(source, _ALIASES["open_interest"])
            ),
            "iv": _finite_number(_column_value(source, _ALIASES["iv"])),
            "delta": _finite_number(_column_value(source, _ALIASES["delta"])),
            "gamma": _finite_number(_column_value(source, _ALIASES["gamma"])),
            "theta": _finite_number(_column_value(source, _ALIASES["theta"])),
            "vega": _finite_number(_column_value(source, _ALIASES["vega"])),
            "rho": _finite_number(_column_value(source, _ALIASES["rho"])),
            "contract_multiplier": multiplier,
            "currency": _column_value(source, _ALIASES["currency"]),
        }
        if require_quotes:
            # 关键报价/风险字段缺失或非有限值时整条链失败关闭；不使用 0、1
            # 或默认乘数填补。``volume``/``open_interest`` 的 0 是有效观测值。
            for field in QUOTE_REQUIRED_COLUMNS:
                value = row[field]
                if field == "currency":
                    if value is None or pd.isna(value) or (isinstance(value, str) and not value.strip()):
                        return None
                else:
                    try:
                        if not math.isfinite(float(value)):
                            return None
                    except (TypeError, ValueError, OverflowError):
                        return None
            if row["spot"] <= 0 or row["bid"] < 0 or row["ask"] < 0:
                return None
            if row["ask"] < row["bid"] or row["last"] < 0:
                return None
            if row["volume"] < 0 or row["open_interest"] < 0 or row["iv"] < 0:
                return None
        else:
            # 保留缺失字段清单，供调用方显式决定是否可交易，而不是静默伪造。
            missing = []
            for field in QUOTE_REQUIRED_COLUMNS:
                value = row[field]
                if field == "currency":
                    is_missing = (
                        value is None
                        or pd.isna(value)
                        or (isinstance(value, str) and not value.strip())
                    )
                else:
                    try:
                        is_missing = value is None or not math.isfinite(float(value))
                    except (TypeError, ValueError, OverflowError):
                        is_missing = True
                if is_missing:
                    missing.append(field)
            missing_quote_fields.update(missing)
        rows.append(row)

    result = pd.DataFrame(rows, columns=OPTION_CHAIN_COLUMNS)
    if result.empty or result["option_symbol"].duplicated().any():
        return None
    result = result.sort_values(
        ["expiry", "strike", "option_type", "option_symbol"],
        kind="mergesort",
    ).reset_index(drop=True)
    result.attrs["schema"] = "quantada.option_chain.v1"
    result.attrs["as_of"] = visibility_time
    result.attrs["quote_complete"] = bool(require_quotes)
    result.attrs["timestamp_source"] = "caller" if used_caller_timestamp else "source"
    if missing_quote_fields:
        result.attrs["missing_quote_fields"] = tuple(sorted(missing_quote_fields))
    return result


__all__ = [
    "OPTION_CHAIN_COLUMNS",
    "CONTRACT_REQUIRED_COLUMNS",
    "QUOTE_REQUIRED_COLUMNS",
    "normalize_option_chain",
]
