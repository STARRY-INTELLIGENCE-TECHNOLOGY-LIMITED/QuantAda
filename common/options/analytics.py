"""期权策略共用的合约解析、估值和时间序列工具。

这些函数只处理纯数据和数学计算，不读取账户、持仓或订单，也不依赖具体 Broker。
策略可以复用同一套 Black-Scholes、价值带和快照字段规则，避免每个期权策略重复实现。
年化波动率和默认统计窗口读取 QuantAda 的 ``config.ANNUAL_FACTOR``，兼容 252 交易日和 365 日历日市场。
"""

import math
import re
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import config as _framework_config
except Exception:
    _framework_config = None


def configured_annual_factor(default: float = 252.0) -> float:
    """读取 QuantAda 的年化因子；独立使用时回退到常用交易日数。"""
    try:
        value = float(getattr(_framework_config, "ANNUAL_FACTOR", default))
    except (TypeError, ValueError, OverflowError):
        value = float(default)
    return value if math.isfinite(value) and value > 0 else float(default)


OPTION_COLUMN_ALIASES = {
    "option_fair_value": (
        "option_fair_value", "option_value", "theoretical_value", "model_value",
        "fair_value", "v_option",
    ),
    "option_lower_band": (
        "option_lower_band", "fair_value_low", "value_low", "lower_band", "lower_value",
    ),
    "option_upper_band": (
        "option_upper_band", "fair_value_high", "value_high", "upper_band", "upper_value",
    ),
    "option_weighted_value": ("option_weighted_value", "option_w", "entry_value"),
    "spot": ("spot", "underlying_price", "underlying_spot", "underlying_close", "s"),
    "underlying": ("underlying", "underlying_symbol", "underlying_code", "underlier"),
    "underlying_fair_value": (
        "underlying_fair_value", "underlying_value", "fundamental_value", "value_v", "v",
    ),
    "underlying_weighted_value": (
        "underlying_weighted_value", "weighted_underlying_value", "weighted_value", "value_w", "w",
    ),
    "strike": ("strike", "strike_price", "exercise_price"),
    "expiry": ("expiry", "expiration", "expiration_date", "strike_time", "expiry_date"),
    "iv": ("iv", "implied_volatility", "implied_vol", "sigma"),
    "historical_volatility": (
        "historical_volatility", "realized_volatility", "historical_vol", "realized_vol", "hv",
    ),
    "iv_percentile": ("iv_percentile", "ivp", "iv_rank", "aggregate_ivp"),
    "delta": ("delta", "option_delta"),
    "bid": ("bid", "bid_price"),
    "ask": ("ask", "ask_price"),
    "confidence": ("confidence", "valuation_confidence"),
    "option_type": ("option_type", "right", "cp", "put_call"),
    "level": ("level", "zone", "buy_level", "valuation_level"),
    "method": ("method", "valuation_method", "model_method"),
    "review_anchor": ("review_anchor", "anchor", "next_review"),
}

_OPTION_SYMBOL_RE = re.compile(
    r"^(?P<underlying>[A-Z0-9][A-Z0-9._-]*?)(?P<expiry>\d{8}|\d{6})"
    r"(?P<right>[CP])(?P<strike>\d{3,9})$",
    re.IGNORECASE,
)

_MARKET_TOKENS = {
    "US", "HK", "SH", "SZ", "SG", "JP", "AU", "CA",
    "SHSE", "SSE", "SZSE", "HKEX", "NASDAQ", "NYSE", "AMEX",
    "ARCA", "IEX", "SMART", "ISLAND", "BATS", "CBOE",
}
_CURRENCY_TOKENS = {"USD", "HKD", "CNY", "CNH"}


def normal_cdf(value: float) -> float:
    """计算标准正态分布累积分布函数。"""
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def safe_number(value, default=np.nan) -> float:
    """把外部元数据转为有限浮点数。"""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def safe_period(value, default: int) -> int:
    """把滚动窗口参数限制为至少两条数据。"""
    try:
        return max(2, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(2, int(default))


def safe_integer(value, default: int) -> int:
    """把整数参数安全转换为有限整数。"""
    number = safe_number(value, float(default))
    return int(number) if math.isfinite(number) else int(default)


def normalise_option_type(value) -> str:
    """统一期权方向名称。"""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    raw = str(value or "").strip().upper()
    if raw in {"P", "PUT", "认沽", "看跌"}:
        return "PUT"
    if raw in {"C", "CALL", "认购", "看涨"}:
        return "CALL"
    return ""


def normalise_option_types(value, default=("CALL", "PUT")) -> set[str]:
    """把策略参数中的期权方向列表标准化。"""
    if isinstance(value, str):
        values = re.split(r"[,\s]+", value.strip().upper())
    else:
        values = [str(item).strip().upper() for item in value or []]
    normalised = {normalise_option_type(item) for item in values}
    return {item for item in normalised if item} or set(default)


def parse_expiry(value):
    """解析六位、八位或常见分隔符格式的期权到期日。"""
    if value is None:
        return pd.NaT
    try:
        if pd.isna(value):
            return pd.NaT
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and np.isnan(value):
        return pd.NaT
    raw = str(value).strip()
    formats = (
        (("%y%m%d", "%Y%m%d") if len(raw) == 6 else ("%Y%m%d", "%y%m%d"))
        + ("%Y-%m-%d", "%Y/%m/%d")
    )
    for fmt in formats:
        try:
            return pd.Timestamp(datetime.strptime(raw, fmt).date())
        except (TypeError, ValueError):
            continue
    try:
        return pd.Timestamp(value).normalize()
    except Exception:
        return pd.NaT


def mad(values: np.ndarray) -> float:
    """计算中位数绝对偏差。"""
    values = values[np.isfinite(values)]
    if values.size == 0:
        return np.nan
    median = np.median(values)
    return float(np.median(np.abs(values - median)))


def series_column(dataframe: pd.DataFrame, aliases, default=np.nan, numeric=True) -> pd.Series:
    """按不区分大小写的别名提取列，没有列时读取 DataFrame.attrs。"""
    columns = {str(column).strip().lower(): column for column in dataframe.columns}
    selected = None
    for alias in aliases:
        if str(alias).lower() in columns:
            selected = dataframe[columns[str(alias).lower()]]
            break

    if selected is None:
        attrs = getattr(dataframe, "attrs", {}) or {}
        for alias in aliases:
            for key, value in attrs.items():
                if str(key).strip().lower() == str(alias).lower():
                    selected = pd.Series(value, index=dataframe.index)
                    break
            if selected is not None:
                break

    if selected is None:
        selected = pd.Series(default, index=dataframe.index)
    else:
        selected = selected.reindex(dataframe.index)

    if numeric:
        return pd.to_numeric(selected, errors="coerce")
    return selected.astype("string")


def parse_option_symbol(symbol: str, strike_scale: float = 1000.0) -> dict:
    """解析常见 venue/OCC 风格代码，显式列字段优先于该猜测结果。"""
    raw = str(symbol or "").strip().upper()
    code = raw.split(".", 1)[1] if "." in raw else raw
    match = _OPTION_SYMBOL_RE.match(code)
    if not match:
        return {}

    expiry = parse_expiry(match.group("expiry"))
    strike_digits = match.group("strike")
    scale = safe_number(strike_scale, 1000.0)
    if scale <= 0:
        scale = 1000.0
    if len(strike_digits) <= 4:
        scale = 1.0
    elif len(strike_digits) == 5:
        scale = 100.0

    return {
        "underlying": match.group("underlying"),
        "expiry": expiry,
        "strike": safe_number(strike_digits) / scale,
        "option_type": normalise_option_type(match.group("right")),
    }


def underlying_key(symbol: str) -> str:
    """把股票代码归一化为可与期权代码匹配的标的键。"""
    raw_name = str(symbol or "").strip().upper()
    parts = [part for part in raw_name.split(".") if part]
    if len(parts) >= 3 and parts[0] in {"STK", "SEC"} and parts[-1] in _CURRENCY_TOKENS:
        return ".".join(parts[1:-1])
    if len(parts) >= 3 and parts[0] in {"MARKET", "VENUE"}:
        return ".".join(parts[2:])
    if len(parts) >= 2 and parts[0] in _MARKET_TOKENS:
        return ".".join(parts[1:])
    if len(parts) >= 2 and parts[-1] in _MARKET_TOKENS:
        return ".".join(parts[:-1])
    return raw_name


def black_scholes_series(
    index,
    spot,
    strike,
    expiry,
    option_type,
    iv,
    risk_free_rate=0.03,
    dividend_yield=0.0,
) -> pd.Series:
    """按行计算 Black-Scholes 欧式期权理论价值。"""
    risk_free = safe_number(risk_free_rate, 0.03)
    dividend = safe_number(dividend_yield, 0.0)
    values = []
    for current_date, s, k, expiry_date, right, sigma in zip(
        index, spot, strike, expiry, option_type, iv,
    ):
        s = safe_number(s)
        k = safe_number(k)
        sigma = safe_number(sigma)
        if not (s > 0 and k > 0 and pd.notna(expiry_date) and sigma > 0):
            values.append(np.nan)
            continue
        time_to_expiry = max(
            0.0,
            (pd.Timestamp(expiry_date) - pd.Timestamp(current_date).normalize()).days / 365.0,
        )
        intrinsic = max(s - k, 0.0) if right == "CALL" else max(k - s, 0.0)
        if time_to_expiry <= 0 or right not in {"CALL", "PUT"}:
            values.append(intrinsic if right in {"CALL", "PUT"} else np.nan)
            continue
        root_t = math.sqrt(time_to_expiry)
        d1 = (
            math.log(s / k)
            + (risk_free - dividend + 0.5 * sigma * sigma) * time_to_expiry
        ) / (sigma * root_t)
        d2 = d1 - sigma * root_t
        if right == "CALL":
            value = (
                s * math.exp(-dividend * time_to_expiry) * normal_cdf(d1)
                - k * math.exp(-risk_free * time_to_expiry) * normal_cdf(d2)
            )
        else:
            value = (
                k * math.exp(-risk_free * time_to_expiry) * normal_cdf(-d2)
                - s * math.exp(-dividend * time_to_expiry) * normal_cdf(-d1)
            )
        values.append(max(0.0, value))
    return pd.Series(values, index=spot.index, dtype="float64")


def iv_percentile(
    dataframe: pd.DataFrame,
    iv: pd.Series,
    window: int | None = None,
    explicit: bool = True,
) -> pd.Series:
    """计算当前 IV 在历史窗口中的百分位，显式 IVP 优先。"""
    if explicit:
        explicit_ivp = series_column(dataframe, OPTION_COLUMN_ALIASES["iv_percentile"], default=np.nan)
        explicit_ivp = explicit_ivp.where(explicit_ivp > 1.0, explicit_ivp * 100.0)
        if explicit_ivp.notna().any():
            return explicit_ivp.clip(lower=0, upper=100)
    annual_factor = configured_annual_factor()
    window = safe_period(
        annual_factor if window is None else window,
        int(round(annual_factor)),
    )
    return iv.rolling(window, min_periods=max(2, min(window, 20))).apply(
        lambda values: float(np.mean(values <= values[-1]) * 100.0)
        if np.isfinite(values[-1]) else np.nan,
        raw=True,
    )


def level_rank(
    distance_to_w: pd.Series,
    l1: float = -0.20,
    l2: float = -0.10,
    l3: float = -0.01,
) -> pd.Series:
    """把现价距 W 的折价映射为可解释的 L1-L4。"""
    values = [safe_number(l1, -0.20), safe_number(l2, -0.10), safe_number(l3, -0.01)]
    if not all(math.isfinite(value) for value in values):
        values = [-0.20, -0.10, -0.01]
    l1, l2, l3 = sorted(values)
    result = pd.Series(0, index=distance_to_w.index, dtype="int64")
    result = result.mask(distance_to_w <= l3, 3)
    result = result.mask(distance_to_w <= l2, 2)
    result = result.mask(distance_to_w <= l1, 1)
    return result.mask(distance_to_w.isna(), 0)


def _snapshot_series_value(series, current_dt, default=np.nan):
    """读取当前时点或其之前最近一条序列值。"""
    if series is None or len(series) == 0:
        return default
    try:
        timestamp = pd.Timestamp(current_dt)
        if isinstance(series.index, pd.DatetimeIndex):
            if series.index.tz is None and timestamp.tzinfo is not None:
                timestamp = timestamp.tz_localize(None)
            elif series.index.tz is not None and timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(series.index.tz)
            position = series.index.searchsorted(timestamp, side="right") - 1
            return series.iloc[position] if position >= 0 else default
        return series.loc[timestamp]
    except Exception:
        return default


def snapshot_value(snapshot, name, current_dt, default=np.nan):
    """读取快照中的数值。"""
    return _snapshot_series_value(snapshot.get(name), current_dt, default)


def snapshot_text(snapshot, name, current_dt) -> str:
    """读取快照中的展示文本，不参与交易决策。"""
    value = _snapshot_series_value(snapshot.get(name), current_dt, "")
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def build_option_snapshot(
    dataframe: pd.DataFrame,
    symbol: str,
    params,
    underlying_close_map=None,
) -> dict:
    """从单一期权 DataFrame 构造确定性的估值和信号基础序列。"""
    underlying_close_map = underlying_close_map or {}
    parsed_index = pd.to_datetime(dataframe.index, errors="coerce")
    if getattr(parsed_index, "tz", None) is not None:
        parsed_index = parsed_index.tz_localize(None)
    index = pd.DatetimeIndex(parsed_index, name="datetime")
    close = pd.to_numeric(dataframe["close"], errors="coerce").reindex(dataframe.index)
    parsed = parse_option_symbol(symbol, strike_scale=getattr(params, "strike_scale", 1000.0))

    option_type = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["option_type"],
        default=parsed.get("option_type", ""),
        numeric=False,
    ).map(normalise_option_type)
    spot = series_column(dataframe, OPTION_COLUMN_ALIASES["spot"], default=np.nan)
    if spot.isna().all() and parsed.get("underlying"):
        underlying_close = underlying_close_map.get(parsed["underlying"].upper())
        if underlying_close is not None:
            spot = underlying_close.reindex(dataframe.index).ffill()
    strike = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["strike"],
        default=parsed.get("strike", np.nan),
    )
    expiry_raw = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["expiry"],
        default=parsed.get("expiry", pd.NaT),
        numeric=False,
    )
    expiry = expiry_raw.map(parse_expiry)

    # 期权没有现货字段时，不能把期权自身的收盘价当成标的价格。
    is_option_contract = bool(parsed.get("option_type"))
    is_option_contract = is_option_contract or option_type.isin({"CALL", "PUT"}).any()
    is_option_contract = is_option_contract or strike.notna().any() or expiry.notna().any()

    explicit_iv = series_column(dataframe, OPTION_COLUMN_ALIASES["iv"], default=np.nan)
    explicit_iv = explicit_iv.where(explicit_iv <= 3.0, explicit_iv / 100.0)
    explicit_hv = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["historical_volatility"],
        default=np.nan,
    )
    explicit_hv = explicit_hv.where(explicit_hv <= 3.0, explicit_hv / 100.0)
    volatility_base = spot.where(spot > 0)
    if not is_option_contract:
        volatility_base = volatility_base.where(volatility_base > 0, close.where(close > 0))
    log_returns = np.log(volatility_base.replace(0, np.nan)).diff()
    annual_factor = configured_annual_factor()
    annual_window = max(2, int(round(annual_factor)))
    raw_value_window = getattr(params, "value_window", None)
    value_window = safe_period(
        annual_window if raw_value_window is None else raw_value_window,
        annual_window,
    )
    default_error_window = max(2, value_window // 4)
    raw_error_window = getattr(params, "error_window", None)
    error_window = safe_period(
        default_error_window if raw_error_window is None else raw_error_window,
        default_error_window,
    )
    raw_iv_window = getattr(params, "iv_window", None)
    iv_window = safe_period(
        value_window if raw_iv_window is None else raw_iv_window,
        value_window,
    )
    requested_min_history = safe_period(getattr(params, "min_history", 60), 60)
    value_min_history = min(requested_min_history, value_window)
    error_min_history = min(requested_min_history, error_window)
    iv_min_history = min(requested_min_history, iv_window)
    realized_iv = (
        log_returns.shift(1)
        .rolling(iv_window, min_periods=iv_min_history)
        .std()
        * math.sqrt(annual_factor)
    )
    model_iv = explicit_hv.where(explicit_hv > 0, realized_iv)
    model_iv = model_iv.where(model_iv > 0, explicit_iv)
    iv = explicit_iv.where(explicit_iv > 0, model_iv)

    option_fair = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["option_fair_value"],
        default=np.nan,
    )
    bs_fair = black_scholes_series(
        index,
        spot,
        strike,
        expiry,
        option_type,
        model_iv,
        risk_free_rate=getattr(params, "risk_free_rate", 0.03),
        dividend_yield=getattr(params, "dividend_yield", 0.0),
    )
    model_value = option_fair.where(option_fair > 0, bs_fair)
    rolling_anchor = close.shift(1).rolling(
        value_window,
        min_periods=value_min_history,
    ).median()
    model_value = model_value.where(model_value > 0, rolling_anchor)

    residual = close - model_value
    error_mad = residual.shift(1).rolling(
        error_window,
        min_periods=error_min_history,
    ).apply(mad, raw=True)
    error_floor = model_value.abs() * max(
        0.0,
        safe_number(getattr(params, "min_band_pct", 0.05), 0.05),
    )
    band_sigma = max(0.0, safe_number(getattr(params, "band_sigma", 1.5), 1.5))
    width = pd.concat([error_mad * 1.4826 * band_sigma, error_floor], axis=1).max(axis=1)
    width = width.where(width > 0, error_floor)

    explicit_lower = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["option_lower_band"],
        default=np.nan,
    )
    explicit_upper = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["option_upper_band"],
        default=np.nan,
    )
    lower_band = explicit_lower.where(explicit_lower >= 0, model_value - width).clip(lower=0)
    upper_band = explicit_upper.where(explicit_upper > 0, model_value + width)
    upper_band = upper_band.where(upper_band >= lower_band, model_value + width)

    underlying_fair = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["underlying_fair_value"],
        default=np.nan,
    ).where(lambda value: value > 0)
    underlying_anchor = spot.shift(1).rolling(
        value_window,
        min_periods=value_min_history,
    ).median()
    underlying_fair = underlying_fair.where(underlying_fair > 0, underlying_anchor)
    underlying_weighted = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["underlying_weighted_value"],
        default=np.nan,
    )
    value_weight = max(0.01, safe_number(getattr(params, "value_weight", 0.8), 0.8))
    underlying_weighted = underlying_weighted.where(
        underlying_weighted > 0,
        underlying_fair * value_weight,
    )
    option_weighted = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["option_weighted_value"],
        default=np.nan,
    )
    option_weighted = option_weighted.where(option_weighted > 0, model_value * value_weight)

    distance_to_w = spot / underlying_weighted.replace(0, np.nan) - 1.0
    level_values = (
        getattr(params, "level_l1_distance", -0.20),
        getattr(params, "level_l2_distance", -0.10),
        getattr(params, "level_l3_distance", -0.01),
    )
    level_values = level_rank(distance_to_w, *level_values)
    iv_percentile_values = iv_percentile(
        dataframe,
        iv,
        window=iv_window,
        explicit=True,
    )
    iv_premium = iv / model_iv.replace(0, np.nan) - 1.0
    delta = series_column(dataframe, OPTION_COLUMN_ALIASES["delta"], default=np.nan)
    bid = series_column(dataframe, OPTION_COLUMN_ALIASES["bid"], default=np.nan)
    ask = series_column(dataframe, OPTION_COLUMN_ALIASES["ask"], default=np.nan)
    midpoint = ((bid + ask) / 2.0).replace(0, np.nan)
    spread_pct = (ask - bid) / midpoint
    dte = pd.Series(
        [
            (expiry_value - current_date.normalize()).days
            if pd.notna(expiry_value) and pd.notna(current_date)
            else np.nan
            for current_date, expiry_value in zip(index, expiry)
        ],
        index=dataframe.index,
        dtype="float64",
    )
    confidence = series_column(dataframe, OPTION_COLUMN_ALIASES["confidence"], default=np.nan)

    indicators = {
        "market_price": close,
        "option_fair_value": model_value,
        "option_lower_band": lower_band,
        "option_upper_band": upper_band,
        "option_weighted_value": option_weighted,
        "spot": spot,
        "underlying_fair_value": underlying_fair,
        "underlying_weighted_value": underlying_weighted,
        "distance_to_w": distance_to_w,
        "level_rank": level_values,
        "iv": iv,
        "model_iv": model_iv,
        "iv_premium": iv_premium,
        "iv_percentile": iv_percentile_values,
        "delta": delta,
        "spread_pct": spread_pct,
        "dte": dte,
        "confidence": confidence,
        "band_width": width,
    }
    explicit_level = series_column(
        dataframe,
        OPTION_COLUMN_ALIASES["level"],
        default="",
        numeric=False,
    )
    explicit_level_rank = explicit_level.map(
        lambda value: safe_number(str(value).upper().lstrip("L"), 0.0)
    )
    indicators["level_rank"] = explicit_level_rank.where(explicit_level_rank > 0, level_values)
    return {
        "indicators": indicators,
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry,
        "method": series_column(
            dataframe,
            OPTION_COLUMN_ALIASES["method"],
            default="",
            numeric=False,
        ),
        "review_anchor": series_column(
            dataframe,
            OPTION_COLUMN_ALIASES["review_anchor"],
            default="",
            numeric=False,
        ),
        "underlying": series_column(
            dataframe,
            OPTION_COLUMN_ALIASES["underlying"],
            default=parsed.get("underlying", ""),
            numeric=False,
        ),
    }
