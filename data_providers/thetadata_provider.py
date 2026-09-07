"""ThetaData 历史行情适配器。

该模块仅负责将 ThetaData 的股票/期权历史接口转换为 QuantAda 数据契约。
令牌和 gRPC 连接均采用惰性初始化；未安装 SDK 或未配置令牌时返回 ``None``，
不会阻断其他 Provider。期权合约使用 OCC 代码解析后请求单一合约，避免把链
快照误当成历史数据。
"""

from __future__ import annotations

import datetime as _dt
import importlib
import math
import os
import re
import threading

import pandas as pd

import config
from common.options.analytics import iv_percentile
from common.options.chain import normalize_option_chain
from .base_provider import BaseDataProvider


THETADATA_API_KEY = os.getenv("THETADATA_API_KEY", "").strip()
# 以下为低频、实现固定的安全默认值；如需调整应通过代码评审修改。
_OPTION_CONTRACT_MULTIPLIER = 100.0
_ENRICH_OPTIONS = True
_REQUEST_TIMEOUT_SECONDS = 20.0
_MAX_AUTO_CHAIN_EXPIRATIONS = 16

_OCC_RE = re.compile(r"^US\.([A-Z0-9]+?)(\d{6})([CP])(\d{8})$", re.IGNORECASE)
_REQUIRED = ("open", "high", "low", "close", "volume")


def _as_date(value):
    """将日期/时间输入转换为 ThetaData 所需的 date。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        raw = str(value).strip()
        if re.fullmatch(r"\d{8}", raw):
            return _dt.datetime.strptime(raw, "%Y%m%d").date()
        return pd.Timestamp(value).date()
    except Exception:
        return None


def _to_pandas(value):
    """兼容 ThetaData pandas、polars 和字典返回值。"""
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, dict):
        try:
            return pd.DataFrame(value)
        except Exception:
            return None
    to_pandas = getattr(value, "to_pandas", None)
    if callable(to_pandas):
        try:
            return to_pandas()
        except Exception:
            return None
    return None


def _column(df, aliases):
    """按大小写不敏感别名取得列名。"""
    lowered = {str(c).lower(): c for c in df.columns}
    for alias in aliases:
        if alias in df.columns:
            return alias
        found = lowered.get(alias.lower())
        if found is not None:
            return found
    return None


class ThetaDataProvider(BaseDataProvider):
    """通过 ThetaData 获取美国股票和期权历史数据。"""

    PRIORITY = 45

    @staticmethod
    def _option_multiplier():
        """读取 Provider 内置的标准期权合约乘数；无效值时返回 None 并安全拒绝估值。"""
        try:
            value = float(_OPTION_CONTRACT_MULTIPLIER)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def __init__(self, client=None, api_key=None):
        self.client = client
        configured = api_key
        if configured is None:
            configured = os.getenv("THETADATA_API_KEY")
        if not configured:
            # 与其它 Provider 一致，命令行 ``--config`` 覆盖在实例化前生效。
            configured = getattr(config, "THETADATA_TOKEN", None)
        if not configured:
            configured = THETADATA_API_KEY
        self.token = str(configured or "").strip()
        self.is_external_mode = (
            client is None
            and (
                not self.token
                or self.token.upper() in {"YOUR_TOKEN_HERE", "EXTERNAL_MODE"}
            )
        )
        self._client_lock = threading.RLock()
        self._client_error = None
        self._unsupported_methods = set()

    def _get_client(self):
        """惰性创建 ThetaClient；不在 Provider 构造阶段执行网络认证。"""
        with self._client_lock:
            if self.client is not None:
                return self.client
            if (
                not self.token
                or self.token.upper() in {"YOUR_TOKEN_HERE", "EXTERNAL_MODE"}
            ):
                return None
            try:
                module = importlib.import_module("thetadata")
                cls = getattr(module, "ThetaClient", None)
                if cls is None:
                    raise ImportError("thetadata.ThetaClient 不存在")
                # pandas 返回值便于与框架既有标准化逻辑衔接。
                timeout = self._timeout_seconds()
                outcome = {}

                def construct():
                    try:
                        outcome["client"] = cls(api_key=self.token, dataframe_type="pandas")
                    except Exception as exc:
                        outcome["error"] = exc

                worker = threading.Thread(target=construct, name="thetadata-client-init", daemon=True)
                worker.start()
                worker.join(timeout)
                if worker.is_alive():
                    raise TimeoutError(f"ThetaData client initialization timed out after {timeout:.1f}s")
                if "error" in outcome:
                    raise outcome["error"]
                self.client = outcome.get("client")
                if self.client is None:
                    raise RuntimeError("ThetaData client initialization returned no client")
                self._client_error = None
                return self.client
            except Exception as exc:
                self._client_error = exc
                print(f"[ThetaData] client unavailable: {exc}; 请解除 requirements.txt 中 thetadata/python-dotenv 注释后安装")
                return None

    @staticmethod
    def _timeout_seconds():
        """读取并限制 Provider 内置的单次 ThetaData 操作超时。"""
        try:
            timeout = float(_REQUEST_TIMEOUT_SECONDS)
        except (TypeError, ValueError, OverflowError):
            timeout = 20.0
        return max(0.1, min(timeout, 300.0))

    @staticmethod
    def _parse_option(symbol):
        """解析 ``US.AAPL240119P00150000`` 为根代码、到期日、方向和行权价。"""
        raw = str(symbol or "").strip().upper()
        match = _OCC_RE.match(raw)
        if not match:
            return None
        root, expiry, right, strike = match.groups()
        try:
            expiry_date = _dt.datetime.strptime(expiry, "%y%m%d").date()
            strike_value = int(strike) / 1000.0
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(strike_value) or strike_value <= 0:
            return None
        return root, expiry_date, f"{strike_value:g}", right

    @staticmethod
    def _normalise_ohlcv(raw):
        """把 ThetaData 表格标准化为 DatetimeIndex + OHLCV。"""
        df = _to_pandas(raw)
        if df is None or df.empty:
            return None
        time_col = _column(df, ("timestamp", "datetime", "time", "time_key", "date", "created"))
        if time_col is None:
            return None
        result = pd.DataFrame(index=df.index)
        result.index = ThetaDataProvider._parse_timestamps(df, time_col)
        for name, aliases in {
            "open": ("open", "open_price"),
            "high": ("high", "high_price"),
            "low": ("low", "low_price"),
            "close": ("close", "close_price", "last", "last_price"),
            "volume": ("volume", "vol", "total_volume"),
        }.items():
            source = _column(df, aliases)
            if source is None:
                return None
            result[name] = pd.to_numeric(df[source], errors="coerce").to_numpy()
        # EOD/Quote 响应若同时携带盘口、OI 或 Greeks，保留这些字段供策略直接使用。
        for name, aliases in {
            "bid": ("bid", "bid_price"), "ask": ("ask", "ask_price"),
            "last": ("last", "last_price"), "open_interest": ("open_interest", "oi"),
            "iv": ("iv", "implied_volatility", "implied_vol"), "delta": ("delta",),
            "gamma": ("gamma",), "theta": ("theta",), "vega": ("vega",), "rho": ("rho",),
        }.items():
            source = _column(df, aliases)
            if source is not None:
                result[name] = pd.to_numeric(df[source], errors="coerce").to_numpy()
        result = result[~result.index.isna()]
        result = result.replace([float("inf"), float("-inf")], pd.NA).dropna(subset=list(_REQUIRED))
        if result.empty:
            return None
        result = result[~result.index.duplicated(keep="last")].sort_index()
        result.index.name = "datetime"
        return result

    @staticmethod
    def _parse_timestamps(df, time_col):
        """解析日期、epoch 毫秒和 ThetaData 的 date + ms_of_day 字段。"""
        source = df[time_col]
        numeric = pd.to_numeric(source, errors="coerce")
        # 纯 YYYYMMDD 数字若直接交给 pandas 会被解释成纳秒时间戳。
        ymd_mask = numeric.between(10_000_000, 99_999_999)
        parsed = pd.to_datetime(source, errors="coerce", utc=True)
        if ymd_mask.any():
            parsed_ymd = pd.to_datetime(source.astype("string"), format="%Y%m%d", errors="coerce", utc=True)
            parsed = parsed.where(~ymd_mask, parsed_ymd)
        # epoch 数值按数量级修正单位。
        epoch_ms_mask = numeric.abs().between(1_000_000_000_000, 9_999_999_999_999)
        if epoch_ms_mask.any():
            parsed_epoch = pd.to_datetime(numeric, unit="ms", errors="coerce", utc=True)
            parsed = parsed.where(~epoch_ms_mask, parsed_epoch)
        epoch_seconds_mask = numeric.abs().between(1_000_000_000, 9_999_999_999)
        if epoch_seconds_mask.any():
            parsed_epoch = pd.to_datetime(numeric, unit="s", errors="coerce", utc=True)
            parsed = parsed.where(~epoch_seconds_mask, parsed_epoch)
        ms_col = _column(df, ("ms_of_day", "milliseconds", "ms"))
        if str(time_col).lower() in {"date", "day"} and ms_col is not None:
            millis = pd.to_numeric(df[ms_col], errors="coerce")
            parsed = parsed + pd.to_timedelta(millis, unit="ms")
        return parsed

    @staticmethod
    def _merge_metadata(base, raw, fields):
        """按时间戳将可选元数据合并到 OHLCV，不因元数据权限失败而丢弃价格。"""
        extra = _to_pandas(raw)
        if extra is None or extra.empty:
            return base
        tcol = _column(extra, ("timestamp", "datetime", "time", "time_key", "date", "created"))
        if tcol is None:
            return base
        idx = ThetaDataProvider._parse_timestamps(extra, tcol)
        valid = ~idx.isna()
        if not valid.any():
            return base
        values = pd.DataFrame(index=idx[valid])
        for output, aliases in fields.items():
            source = _column(extra, aliases)
            if source is not None:
                values[output] = pd.to_numeric(extra.loc[valid, source], errors="coerce").to_numpy()
        if values.empty:
            return base
        values.index.name = "datetime"
        values = values[~values.index.duplicated(keep="last")].sort_index()
        left = base.reset_index().sort_values("datetime")
        right = values.reset_index().sort_values("datetime")
        left["datetime"] = pd.to_datetime(left["datetime"], errors="coerce", utc=True).dt.tz_localize(None)
        right["datetime"] = pd.to_datetime(right["datetime"], errors="coerce", utc=True).dt.tz_localize(None)
        left["datetime"] = left["datetime"].astype("datetime64[ns]")
        right["datetime"] = right["datetime"].astype("datetime64[ns]")
        overlap = [column for column in right.columns if column in left.columns and column != "datetime"]
        right = right.rename(columns={column: f"{column}__extra" for column in overlap})
        merged = pd.merge_asof(left, right, on="datetime", direction="backward", tolerance=pd.Timedelta(days=1))
        for column in overlap:
            extra_column = f"{column}__extra"
            if extra_column in merged.columns:
                merged[column] = merged[column].where(merged[column].notna(), merged[extra_column])
                merged = merged.drop(columns=[extra_column])
        merged["datetime"] = pd.to_datetime(merged["datetime"], errors="coerce", utc=True)
        merged = merged.set_index("datetime")
        merged.index.name = "datetime"
        return merged

    def _call(self, method, **kwargs):
        """执行 SDK 调用并将权限、网络和空数据错误降级为 None。"""
        client = self._get_client()
        if client is None:
            return None
        if method in self._unsupported_methods:
            return None
        timeout = self._timeout_seconds()
        try:
            callable_method = getattr(client, method)
        except Exception as exc:
            print(f"[ThetaData] {method} failed: {exc}")
            return None
        outcome = {}

        def invoke():
            try:
                outcome["value"] = callable_method(**kwargs)
            except Exception as exc:  # SDK 异常只影响当前标的
                outcome["error"] = exc

        worker = threading.Thread(target=invoke, name="thetadata-request", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            print(f"[ThetaData] {method} timed out after {timeout:.1f}s")
            return None
        if "error" in outcome:
            error = outcome["error"]
            message = str(error)
            # 权限错误对同一订阅在本次运行中是稳定的，记忆方法名可
            # 避免自动链查询在数十个到期日上重复发起必然失败的 RPC。
            if "permission_denied" in message.lower() or "professional subscription" in message.lower():
                self._unsupported_methods.add(method)
            print(f"[ThetaData] {method} failed: {error}")
            return None
        return outcome.get("value")

    def _chunked_history(self, method, start, end, **kwargs):
        """按 ThetaData 的约一个月跨日限制拆分分钟历史请求。"""
        frames = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + _dt.timedelta(days=30) - _dt.timedelta(days=1))
            frame = self._call(
                method,
                start_date=cursor,
                end_date=chunk_end,
                **kwargs,
            )
            converted = _to_pandas(frame)
            if converted is not None and not converted.empty:
                frames.append(converted)
            cursor = chunk_end + _dt.timedelta(days=1)
        return pd.concat(frames, ignore_index=True, sort=False) if frames else None

    def get_data(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
        timeframe: str = "Days",
        compression: int = 1,
    ) -> pd.DataFrame:
        """获取股票或单一期权的历史 OHLCV；未来区间只返回已存在的历史部分。"""
        normalized = str(symbol or "").strip().upper()
        if "." in normalized and not normalized.startswith("US."):
            print(f"[ThetaData] unsupported market symbol: {symbol}")
            return None
        if normalized.startswith("US."):
            code = normalized[3:]
        else:
            code = normalized
            normalized = f"US.{code}" if code else ""
        if not code:
            return None
        start = _as_date(start_date)
        end = _as_date(end_date)
        if start is None and end is None:
            end = _dt.date.today()
            start = end - _dt.timedelta(days=365)
        elif start is None:
            start = end - _dt.timedelta(days=365)
        elif end is None:
            end = start + _dt.timedelta(days=365)
        if start > end:
            return None
        today = _dt.date.today()
        requested_end = end
        if start > today:
            return None
        if end > today:
            # 历史接口不能提供未来 K 线；只请求已闭合的历史边界，并在 attrs 留痕。
            end = today
        is_intraday = str(timeframe or "").strip().lower() in {"minutes", "minute", "min", "m"}
        try:
            period = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            return None
        if period <= 0:
            return None
        parsed = self._parse_option(normalized)
        if parsed:
            root, expiry, strike, right = parsed
            if str(timeframe or "").strip().lower() in {"days", "day", "d"} and period == 1:
                raw = self._call(
                    "option_history_eod",
                    start_date=start,
                    end_date=end,
                    symbol=root,
                    expiration=expiry,
                    strike=strike,
                    right=right,
                )
            elif is_intraday and period in {1, 5, 15, 60}:
                raw = self._chunked_history(
                    "option_history_ohlc",
                    start,
                    end,
                    symbol=root,
                    expiration=expiry,
                    interval=f"{period}m",
                    strike=strike,
                    right=right,
                )
            else:
                print("[ThetaData] option history supports Days or 1/5/15/60 Minutes only")
                return None
        else:
            if str(timeframe or "").strip().lower() in {"days", "day", "d"} and period == 1:
                raw = self._call("stock_history_eod", start_date=start, end_date=end, symbol=code)
            elif is_intraday and period in {1, 5, 15, 60}:
                raw = self._chunked_history(
                    "stock_history_ohlc",
                    start,
                    end,
                    symbol=code,
                    interval=f"{period}m",
                )
            else:
                print("[ThetaData] stock history supports Days or 1/5/15/60 Minutes only")
                return None
        result = self._normalise_ohlcv(raw)
        if result is None:
            return None
        if requested_end > today:
            result.attrs["requested_end"] = pd.Timestamp(requested_end, tz="UTC")
            result.attrs["history_end"] = pd.Timestamp(today, tz="UTC")
            result.attrs["future_data_unavailable"] = True
        if parsed:
            multiplier = self._option_multiplier()
            if multiplier is None:
                print("[ThetaData] option contract multiplier is invalid; history rejected")
                return None
            result.attrs["option_symbol"] = normalized
            result.attrs["contract_multiplier"] = multiplier
            result.attrs["contract_multiplier_source"] = "standard_market_assumption"
            result["contract_multiplier"] = multiplier
            # Greeks 是独立接口；失败时价格仍然可用于静态回测。
            if _ENRICH_OPTIONS:
                root, expiry, strike, right = parsed
                # 分钟数据应请求同频的一阶 Greeks；EOD 时间戳通常晚于盘中
                # K 线，直接回填会造成整段盘口 Greeks 错位为 NaN。
                greeks_method = (
                    "option_history_greeks_eod"
                    if not is_intraday
                    else "option_history_greeks_first_order"
                )
                greeks_kwargs = {
                    "symbol": root,
                    "expiration": expiry,
                    "start_date": start,
                    "end_date": end,
                    "strike": strike,
                    "right": right,
                }
                if is_intraday:
                    greeks_kwargs["interval"] = f"{period}m"
                greeks = self._call(
                    greeks_method,
                    **greeks_kwargs,
                )
                result_attrs = dict(result.attrs)
                result = self._merge_metadata(
                    result,
                    greeks,
                    {
                        "iv": ("iv", "implied_volatility", "implied_vol"),
                        "delta": ("delta",),
                        "gamma": ("gamma",),
                        "theta": ("theta",),
                        "vega": ("vega",),
                        "rho": ("rho",),
                    },
                )
                open_interest = self._call(
                    "option_history_open_interest",
                    symbol=root,
                    expiration=expiry,
                    start_date=start,
                    end_date=end,
                    strike=strike,
                    right=right,
                )
                result = self._merge_metadata(
                    result,
                    open_interest,
                    {"open_interest": ("open_interest", "oi")},
                )
                result.attrs.update(result_attrs)
                result.attrs["option_symbol"] = normalized
                result.attrs["contract_multiplier"] = multiplier
                result.attrs["contract_multiplier_source"] = "standard_market_assumption"
                result["contract_multiplier"] = multiplier
                # ThetaData Greeks 提供 IV 但不直接提供 IVP；按历史前缀
                # 计算可复现的百分位，供 CSP 等策略使用，避免引入未来数据。
                iv_series = (
                    pd.to_numeric(result["iv"], errors="coerce")
                    if "iv" in result.columns
                    else pd.Series(float("nan"), index=result.index)
                )
                result["iv_percentile"] = iv_percentile(
                    result,
                    iv_series,
                    window=252,
                    explicit=False,
                )
        if not is_intraday:
            # EOD 接口的 ``created``/``timestamp`` 是成交或生成时刻，股票与
            # 期权通常相差数秒；统一到交易日午夜，便于多数据源按日对齐。
            attrs = dict(result.attrs)
            result.index = pd.DatetimeIndex(result.index).normalize()
            result = result[~result.index.duplicated(keep="last")].sort_index()
            result.index.name = "datetime"
            result.attrs.update(attrs)
        return result

    def get_option_chain(self, underlying, expirations=None, normalized=False, as_of=None, start=None, end=None):
        """获取 ThetaData 期权链快照；历史回测应传入当日可见的 ``as_of``。"""
        if (start is not None or end is not None) and as_of is None:
            print("[ThetaData] historical option-chain requests require as_of to prevent lookahead")
            return None
        raw_underlying = str(underlying or "").strip().upper()
        if "." in raw_underlying and not raw_underlying.startswith("US."):
            return None
        code = raw_underlying.removeprefix("US.")
        if not code:
            return None
        if expirations is None:
            raw_expirations = self._call("option_list_expirations", symbol=code)
            exp_df = _to_pandas(raw_expirations)
            if exp_df is None or exp_df.empty:
                return None
            exp_col = _column(exp_df, ("expiration", "expiry", "date"))
            if exp_col is None:
                return None
            expiration_values = ThetaDataProvider._parse_timestamps(exp_df, exp_col)
            expirations = [x.date() for x in expiration_values.dropna().tolist()]
            # 该接口返回从上市至今的全部到期日。自动发现时只请求仍可能
            # 有快照的近月合约，避免标准订阅在数百个历史到期日上重复失败。
            if len(expirations) > _MAX_AUTO_CHAIN_EXPIRATIONS:
                today = _dt.date.today()
                future = sorted({value for value in expirations if value >= today})
                if future:
                    expirations = future[:_MAX_AUTO_CHAIN_EXPIRATIONS]
        rows = []
        for expiration in expirations:
            frame = None
            # Greeks all 需要 Professional；Standard 订阅仍可使用一阶 Greeks，
            # 再由 OHLC/OI 快照补齐成交与未平仓量字段。
            for base_method in (
                "option_snapshot_greeks_all",
                "option_snapshot_greeks_first_order",
                "option_snapshot_quote",
            ):
                raw = self._call(
                    base_method,
                    symbol=code,
                    expiration=_as_date(expiration),
                    strike="*",
                    right="both",
                )
                candidate = _to_pandas(raw)
                if candidate is not None and not candidate.empty:
                    frame = candidate
                    break
            if frame is not None and not frame.empty:
                frame = self._prepare_chain_frame(frame, code, _as_date(expiration))
                for enrich_method in (
                    "option_snapshot_quote",
                    "option_snapshot_ohlc",
                    "option_snapshot_open_interest",
                ):
                    enrich_raw = self._call(
                        enrich_method,
                        symbol=code,
                        expiration=_as_date(expiration),
                        strike="*",
                        right="both",
                    )
                    enrich = _to_pandas(enrich_raw)
                    if enrich is not None and not enrich.empty:
                        frame = self._merge_chain_columns(
                            frame,
                            self._prepare_chain_frame(enrich, code, _as_date(expiration)),
                        )
                rows.append(frame)
        if not rows:
            return None
        chain = pd.concat(rows, ignore_index=True, sort=False)
        if not normalized:
            return chain
        # 历史链必须由源数据提供 timestamp；实时链才允许使用抓取时间留痕。
        timestamp = None if as_of is not None else pd.Timestamp.now(tz="UTC")
        multiplier = self._option_multiplier()
        if multiplier is None:
            return None
        normalized_chain = normalize_option_chain(
            chain,
            f"US.{code}",
            timestamp=timestamp,
            as_of=as_of,
            contract_multiplier=multiplier,
            require_quotes=True,
        )
        if normalized_chain is not None:
            return normalized_chain
        # Greeks 快照可能不含 OI/成交量等字段；明确降级为“仅合约发现”，
        # attrs 会记录缺失字段，调用方不得将其直接用于交易。
        return normalize_option_chain(
            chain,
            f"US.{code}",
            timestamp=timestamp,
            as_of=as_of,
            contract_multiplier=multiplier,
            require_quotes=False,
        )

    def _option_request(self, symbol, method, start_date=None, end_date=None, timeframe="Days", compression=1):
        """请求单一期权的 Quote、OI 或 Greeks 原始表，供研究工具按需调用。"""
        parsed = self._parse_option(symbol)
        if parsed is None:
            return None
        root, expiry, strike, right = parsed
        start = _as_date(start_date) or expiry - _dt.timedelta(days=365)
        end = _as_date(end_date) or _dt.date.today()
        if start > end:
            return None
        normalized = str(timeframe or "").strip().lower()
        try:
            period = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            return None
        if method.endswith("_eod") or method == "option_history_open_interest":
            raw = self._call(
                method,
                symbol=root,
                expiration=expiry,
                start_date=start,
                end_date=end,
                strike=strike,
                right=right,
            )
        elif normalized in {"minutes", "minute", "min", "m"} and period in {1, 5, 15, 60}:
            raw = self._chunked_history(
                method,
                start,
                end,
                symbol=root,
                expiration=expiry,
                interval=f"{period}m",
                strike=strike,
                right=right,
            )
        else:
            return None
        frame = _to_pandas(raw)
        if frame is None or frame.empty:
            return None
        frame.attrs["option_symbol"] = str(symbol).strip().upper()
        return frame

    def get_option_history_quote(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        """获取单一期权历史买卖报价。"""
        normalized = str(timeframe or "").strip().lower()
        if normalized in {"days", "day", "d"}:
            # ThetaData 没有可由 OHLC EOD 替代的历史 NBBO；避免把收盘价伪装成 bid/ask。
            return None
        method = "option_history_quote"
        return self._option_request(symbol, method, start_date, end_date, timeframe, compression)

    def get_option_history_open_interest(self, symbol, start_date=None, end_date=None):
        """获取单一期权历史未平仓量。"""
        return self._option_request(symbol, "option_history_open_interest", start_date, end_date, "Days", 1)

    def get_option_history_greeks(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        """获取单一期权历史 Greeks 与隐含波动率。"""
        method = (
            "option_history_greeks_eod"
            if str(timeframe or "").lower() in {"days", "day", "d"}
            else "option_history_greeks_first_order"
        )
        return self._option_request(symbol, method, start_date, end_date, timeframe, compression)

    @staticmethod
    def _prepare_chain_frame(frame, root, expiration):
        """补齐 ThetaData 链返回中常见的合约字段，不填充报价或风险数值。"""
        result = frame.copy()
        # ``symbol`` 在 ThetaData 响应中通常是根标的，不可直接当作期权合约代码。
        symbol_col = _column(result, ("option_symbol", "option_code", "contract", "code"))
        if symbol_col is not None and symbol_col != "option_symbol":
            result["option_symbol"] = result[symbol_col]
        exp_col = _column(result, ("expiration", "expiry", "expiry_date"))
        if exp_col is None and expiration is not None:
            result["expiry"] = expiration
        elif exp_col is not None:
            # expiration 可能是 YYYYMMDD 整数，统一通过安全日期解析避免 1970 纳秒误读。
            result["expiry"] = ThetaDataProvider._parse_timestamps(result, exp_col)
        strike_col = _column(result, ("strike", "strike_price", "exercise_price"))
        right_col = _column(result, ("right", "option_type", "call_put", "cp", "type"))
        if "option_symbol" not in result.columns and strike_col and right_col and expiration:
            def make_symbol(row):
                try:
                    strike = float(row[strike_col])
                    right = str(row[right_col]).strip().upper()
                    right = "C" if right in {"C", "CALL", "1"} else "P" if right in {"P", "PUT", "2"} else ""
                    if not right or not math.isfinite(strike) or strike <= 0:
                        return None
                    return f"US.{root}{expiration.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"
                except (TypeError, ValueError, OverflowError):
                    return None
            result["option_symbol"] = result.apply(make_symbol, axis=1)
        if "timestamp" not in result.columns:
            for candidate in ("time", "time_key", "created", "updated"):
                if candidate in result.columns:
                    result["timestamp"] = result[candidate]
                    break
        return result

    @staticmethod
    def _merge_chain_columns(base, extra):
        """按 option_symbol 合并链快照字段，优先保留 Greeks 表已有值。"""
        if "option_symbol" not in base.columns or "option_symbol" not in extra.columns:
            return base
        left = base.copy().set_index("option_symbol")
        right = extra.copy().set_index("option_symbol")
        for column in right.columns:
            if column not in left.columns:
                left[column] = right[column]
            else:
                left[column] = left[column].where(left[column].notna(), right[column])
        return left.reset_index()

    def close(self):
        """关闭 ThetaData 客户端（若 SDK 提供 close）。"""
        with self._client_lock:
            client, self.client = self.client, None
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass


__all__ = ["ThetaDataProvider"]
