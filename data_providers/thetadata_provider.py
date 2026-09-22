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
from zoneinfo import ZoneInfo

import pandas as pd

import config
from common.options.analytics import iv_percentile
from common.options.chain import normalize_option_chain
from .base_provider import BaseDataProvider, REQUEST_ATTEMPTS


THETADATA_API_KEY = os.getenv("THETADATA_API_KEY", "").strip()
# 以下为低频、实现固定的安全默认值；如需调整应通过代码评审修改。
_OPTION_CONTRACT_MULTIPLIER = 100.0
_ENRICH_OPTIONS = True
_REQUEST_TIMEOUT_SECONDS = 60.0
_BULK_REQUEST_TIMEOUT_SECONDS = 120.0
_MAX_AUTO_CHAIN_EXPIRATIONS = 16
_AUTO_CHAIN_STRIKE_RANGE = 40
_DEFAULT_AUTO_MAX_DTE = 90
_EOD_RETRY_CHUNK_DAYS = 365
_EOD_MAX_REQUEST_DAYS = 365
_INTRADAY_CHUNK_DAYS = 30
# 同一进程只允许一个 Theta session；并发只复用该 session 的 gRPC 通道。
_MAX_CONCURRENT_CALLS = 4

_OCC_RE = re.compile(r"^US\.([A-Z0-9]+?)(\d{6})([CP])(\d{8})$", re.IGNORECASE)
_REQUIRED = ("open", "high", "low", "close", "volume")


_VENDOR_TZ = ZoneInfo("America/New_York")


def _vendor_today():
    """Theta 历史接口按美东日历判断今天，避免本地日期把美东未结束交易日当成未来。"""
    return _dt.datetime.now(_VENDOR_TZ).date()


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


def _bounded_int(value, default=None):
    """把可选整数配置转成 int；无效时返回默认值。"""
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_chain_right(value):
    """把链查询方向规范为 ThetaData 的 put/call/both。"""
    text = str(value or "both").strip().lower()
    if text in {"p", "put"}:
        return "put"
    if text in {"c", "call"}:
        return "call"
    return "both"


def _is_empty_theta_error(error):
    """识别 Theta SDK 的空结果异常；这类失败没有可操作细节。"""
    return "no data found" in str(error or "").lower()


def _is_current_day_theta_error(error):
    """当日 EOD/全链 wildcard 会被 Theta 拒绝，不属瞬时失败。"""
    return "cannot fetch current-day data" in str(error or "").lower()


def _is_invalid_theta_argument(error):
    """识别请求形状错误；INVALID_ARGUMENT 重试不会变成功。"""
    text = str(error or "").lower()
    return "invalid_argument" in text or "statuscode.invalid_argument" in text


def _clip_option_history_window(start, end, expiry):
    """把单一期权请求裁到合约寿命，并保证不超过 EOD 接口的最大跨度。"""
    if expiry is None:
        return start, end
    max_span = max(1, int(_EOD_MAX_REQUEST_DAYS) - 1)
    life_start = expiry - _dt.timedelta(days=max_span)
    life_end = expiry
    if start is None or start < life_start:
        start = life_start
    if end is None or end > life_end:
        end = life_end
    return start, end


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


def _is_adjusted_option_response(raw) -> bool:
    """识别 ThetaData 响应中明确标记的调整后期权合约。"""
    frame = _to_pandas(raw)
    values = []
    if isinstance(frame, pd.DataFrame):
        for name in (
            'is_adjusted', 'adjusted', 'adjustment', 'contract_adjusted',
            'deliverable', 'contract_type',
        ):
            column = _column(frame, (name,))
            if column is not None:
                for value in frame[column].tolist():
                    if name == 'deliverable':
                        try:
                            if math.isfinite(float(value)) and float(value) != 100.0:
                                return True
                        except (TypeError, ValueError, OverflowError):
                            pass
                    values.append(value)
        attrs = getattr(frame, 'attrs', {}) or {}
        for key, value in attrs.items():
            if str(key).strip().lower() in {
                'is_adjusted', 'adjusted', 'adjustment', 'contract_adjusted',
                'deliverable', 'contract_type',
            }:
                items = value if isinstance(value, (list, tuple)) else [value]
                if str(key).strip().lower() == 'deliverable':
                    for item in items:
                        try:
                            if math.isfinite(float(item)) and float(item) != 100.0:
                                return True
                        except (TypeError, ValueError, OverflowError):
                            pass
                values.extend(items)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).strip().lower() in {
                'is_adjusted', 'adjusted', 'adjustment', 'contract_adjusted',
                'deliverable', 'contract_type',
            }:
                values.extend(value if isinstance(value, (list, tuple)) else [value])
    for value in values:
        if isinstance(value, bool) and value:
            return True
        text = str(value or '').strip().lower()
        if text in {'true', 'yes', 'y', 'adjusted', 'adjust'} or 'adjusted' in text:
            return True
    return False


def _response_option_multiplier(raw):
    """读取响应中明确提供的期权现金乘数。"""
    frame = _to_pandas(raw)
    if not isinstance(frame, pd.DataFrame):
        return None
    column = _column(frame, (
        'option_contract_multiplier', 'option_contract_size',
        'contract_multiplier', 'contract_size',
    ))
    if column is None:
        return None
    for value in reversed(frame[column].tolist()):
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return None


class ThetaDataProvider(BaseDataProvider):
    """通过 ThetaData 获取美国股票和期权历史数据。"""

    PRIORITY = 45
    HISTORICAL_OPTION_CHAIN = True

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
        self._call_sema = threading.BoundedSemaphore(_MAX_CONCURRENT_CALLS)
        self._stats_lock = threading.Lock()
        self._thread_stats = threading.local()
        self._inflight = 0
        self._client_stale = False
        self._client_error = None
        self._unsupported_methods = set()
        self._owns_client = client is None
        self._expiration_cache = {}
        self._empty_result_count = 0
        self._retryable_fail_count = 0

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
            timeout = self._timeout_seconds()
            try:
                module = importlib.import_module("thetadata")
                cls = getattr(module, "ThetaClient", None)
                if cls is None:
                    raise ImportError("thetadata.ThetaClient 不存在")
            except Exception as exc:
                self._client_error = exc
                print(f"[ThetaData] SDK unavailable: {exc}; 请安装 requirements.txt 中的 thetadata/python-dotenv")
                return None

            last_error = None
            for attempt in range(1, REQUEST_ATTEMPTS + 1):
                outcome = {}
                state = {"timed_out": False}

                def construct():
                    try:
                        client = cls(api_key=self.token, dataframe_type="pandas")
                        if state["timed_out"]:
                            close = getattr(client, "close", None)
                            if callable(close):
                                close()
                        else:
                            outcome["client"] = client
                    except Exception as exc:
                        outcome["error"] = exc

                worker = threading.Thread(target=construct, name="thetadata-client-init", daemon=True)
                worker.start()
                worker.join(timeout)
                if worker.is_alive():
                    state["timed_out"] = True
                    last_error = TimeoutError(
                        f"ThetaData client initialization timed out after {timeout:.1f}s"
                    )
                    # 未结束的 SDK 构造线程可能仍持有 gRPC 连接，不能并发创建第二个 session。
                    worker.join(1.0)
                    if worker.is_alive():
                        print(
                            f"[ThetaData] client initialization attempt {attempt}/{REQUEST_ATTEMPTS} "
                            f"timed out after {timeout:.1f}s; retry skipped because the SDK thread is still active."
                        )
                        break
                elif "error" in outcome:
                    last_error = outcome["error"]
                    print(
                        f"[ThetaData] client initialization attempt {attempt}/{REQUEST_ATTEMPTS} "
                        f"failed: {last_error}"
                    )
                else:
                    client = outcome.get("client")
                    if client is not None:
                        self.client = client
                        self._client_error = None
                        return client
                    last_error = RuntimeError("ThetaData client initialization returned no client")

                if attempt < REQUEST_ATTEMPTS:
                    print(f"[ThetaData] retrying client initialization ({attempt + 1}/{REQUEST_ATTEMPTS})")

            self._client_error = last_error
            print(
                f"[ThetaData] client initialization failed after {REQUEST_ATTEMPTS} attempts: "
                f"{last_error}. Check HTTP_PROXY/HTTPS_PROXY and network access; "
                "the next data request will start a new initialization attempt."
            )
            return None

    @staticmethod
    def _is_bulk_kwargs(kwargs) -> bool:
        """识别全链/全到期日等大载荷请求，以便使用更长的有界超时。"""
        if "strike" in kwargs:
            strike = str(kwargs.get("strike") or "").strip()
            if strike in {"", "*"}:
                return True
        expiration = kwargs.get("expiration")
        if expiration is not None and str(expiration).strip() in {"", "*"}:
            return True
        return False

    @staticmethod
    def _timeout_seconds(bulk=False):
        """读取并限制 Provider 内置的单次 ThetaData 操作超时。"""
        source = _BULK_REQUEST_TIMEOUT_SECONDS if bulk else _REQUEST_TIMEOUT_SECONDS
        fallback = 120.0 if bulk else 60.0
        try:
            timeout = float(source)
        except (TypeError, ValueError, OverflowError):
            timeout = fallback
        return max(0.1, min(timeout, 300.0))

    def _reset_owned_client_locked(self):
        """在已持有客户端锁时关闭自建 session，避免并发认证换出新的 session id。"""
        if not self._owns_client:
            return
        client, self.client = self.client, None
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _reset_owned_client(self):
        """超时后丢掉自建 gRPC 客户端，避免挂起流拖慢后续请求。"""
        with self._client_lock:
            self._reset_owned_client_locked()

    def _mark_client_stale(self, reset_if_exclusive=False):
        """会话失效时优先标记；仅当没有其它 in-flight 请求时才关闭并允许重认证。"""
        with self._client_lock:
            if reset_if_exclusive and self._inflight <= 1:
                self._reset_owned_client_locked()
                self._client_stale = False
                return True
            self._client_stale = True
            return False

    def _is_unsupported(self, method):
        """线程安全地读取 SDK 方法黑名单。"""
        with self._client_lock:
            return method in self._unsupported_methods

    def _mark_unsupported(self, method):
        """线程安全地记录当前订阅不支持的 SDK 方法。"""
        with self._client_lock:
            self._unsupported_methods.add(method)

    def _bump_empty(self):
        """累计空结果；进程计数与当前线程计数分开，避免并发互相偷计数。"""
        self._thread_stats.empty = int(getattr(self._thread_stats, "empty", 0) or 0) + 1
        with self._stats_lock:
            self._empty_result_count += 1

    def _bump_fail(self):
        """累计超时/瞬时失败；供进度行和后置补偿使用。"""
        self._thread_stats.fail = int(getattr(self._thread_stats, "fail", 0) or 0) + 1
        with self._stats_lock:
            self._retryable_fail_count += 1

    def _take_thread_or_global(self, thread_attr, global_attr):
        """优先取出当前线程计数，没有则退回进程计数。"""
        local = int(getattr(self._thread_stats, thread_attr, 0) or 0)
        setattr(self._thread_stats, thread_attr, 0)
        with self._stats_lock:
            global_value = int(getattr(self, global_attr) or 0)
            if local:
                setattr(self, global_attr, max(0, global_value - local))
                return local
            setattr(self, global_attr, 0)
            return global_value

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
        """执行 SDK 调用；同一 session 允许有界并发，超时和瞬时错误有界重试。"""
        if self._is_unsupported(method):
            return None
        timeout = self._timeout_seconds(bulk=self._is_bulk_kwargs(kwargs))
        last_error = None
        timed_out = False
        acquired = False
        counted = False
        try:
            self._call_sema.acquire()
            acquired = True
            with self._client_lock:
                if self._client_stale and self._inflight == 0:
                    self._reset_owned_client_locked()
                    self._client_stale = False
                self._inflight += 1
                counted = True
            for _attempt in range(REQUEST_ATTEMPTS):
                if self._is_unsupported(method):
                    return None
                client = self._get_client()
                if client is None:
                    return None
                try:
                    callable_method = getattr(client, method)
                except Exception as exc:
                    print(f"[ThetaData] {method} failed: {exc}")
                    return None
                outcome = {}

                def invoke():
                    try:
                        outcome["value"] = callable_method(**kwargs)
                    except Exception as exc:
                        outcome["error"] = exc

                worker = threading.Thread(target=invoke, name="thetadata-request", daemon=True)
                worker.start()
                worker.join(timeout)
                if worker.is_alive():
                    # 其它请求仍在使用当前 session 时不能重认证，否则会触发不同 session id 拦截。
                    exclusive = self._mark_client_stale(reset_if_exclusive=True)
                    timed_out = True
                    last_error = None
                    if exclusive:
                        continue
                    break
                if "error" in outcome:
                    error = outcome["error"]
                    message = str(error)
                    if "permission_denied" in message.lower() or "professional subscription" in message.lower():
                        self._mark_unsupported(method)
                        print(f"[ThetaData] {method} failed: {error}")
                        return None
                    if _is_empty_theta_error(error) or _is_current_day_theta_error(error):
                        self._bump_empty()
                        return None
                    if _is_invalid_theta_argument(error):
                        print(f"[ThetaData] {method} failed: {error}")
                        self._bump_fail()
                        return None
                    lowered = message.lower()
                    if "invalid session id" in lowered or "unauthenticated" in lowered:
                        exclusive = self._mark_client_stale(reset_if_exclusive=True)
                        last_error = error
                        timed_out = False
                        if exclusive:
                            continue
                        break
                    last_error = error
                    timed_out = False
                    continue
                return outcome.get("value")
            if timed_out or last_error is None:
                print(f"[ThetaData] {method} timed out after {timeout:.1f}s")
            else:
                print(f"[ThetaData] {method} failed: {last_error}")
            self._bump_fail()
            return None
        finally:
            if counted:
                with self._client_lock:
                    self._inflight = max(0, int(self._inflight) - 1)
                    if self._client_stale and self._inflight == 0:
                        self._reset_owned_client_locked()
                        self._client_stale = False
            if acquired:
                self._call_sema.release()

    def take_empty_result_count(self):
        """取出并清零空结果计数，供 OptionUniverse 进度行汇总。"""
        return self._take_thread_or_global("empty", "_empty_result_count")

    def take_retryable_fail_count(self):
        """取出并清零超时/瞬时失败计数，供进度行与后置补偿使用。"""
        return self._take_thread_or_global("fail", "_retryable_fail_count")

    def _chunked_history(self, method, start, end, chunk_days=_INTRADAY_CHUNK_DAYS, **kwargs):
        """按跨日限制拆分历史请求；分钟默认约一个月，EOD 失败重试可用年块。"""
        try:
            span = max(1, int(chunk_days or _INTRADAY_CHUNK_DAYS))
        except (TypeError, ValueError, OverflowError):
            span = _INTRADAY_CHUNK_DAYS
        frames = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + _dt.timedelta(days=span) - _dt.timedelta(days=1))
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

    def _history_or_chunk(self, method, start, end, chunk_days, **kwargs):
        """跨度达到接口上限时直接分块，避免先打一次必然失败的超限请求。"""
        if start is None or end is None:
            return None
        try:
            width = max(1, int(chunk_days))
        except (TypeError, ValueError, OverflowError):
            width = _EOD_MAX_REQUEST_DAYS
        if (end - start).days >= width:
            return self._chunked_history(method, start, end, chunk_days=width, **kwargs)
        fails_before = int(self._retryable_fail_count)
        raw = self._call(method, start_date=start, end_date=end, **kwargs)
        converted = _to_pandas(raw)
        if converted is not None and not converted.empty:
            return converted
        if self._retryable_fail_count > fails_before and (end - start).days > 1:
            return self._chunked_history(method, start, end, chunk_days=width, **kwargs)
        return converted

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
            end = _vendor_today()
            start = end - _dt.timedelta(days=365)
        elif start is None:
            start = end - _dt.timedelta(days=365)
        elif end is None:
            end = start + _dt.timedelta(days=365)
        if start > end:
            return None
        today = _vendor_today()
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
            start, end = _clip_option_history_window(start, end, expiry)
            if start > end:
                return None
            if str(timeframe or "").strip().lower() in {"days", "day", "d"} and period == 1:
                raw = self._history_or_chunk(
                    "option_history_eod",
                    start,
                    end,
                    _EOD_RETRY_CHUNK_DAYS,
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
                raw = self._history_or_chunk(
                    "stock_history_eod",
                    start,
                    end,
                    _EOD_RETRY_CHUNK_DAYS,
                    symbol=code,
                )
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
        response_multiplier = _response_option_multiplier(raw) if parsed else None
        if parsed and _is_adjusted_option_response(raw) and response_multiplier is None:
            # ThetaData 当前不提供调整后合约的可靠现金乘数；不能静默套用
            # 标准美股期权的 100 倍假设。
            print('[ThetaData] adjusted option contract lacks a verified multiplier; history rejected')
            return None
        result = self._normalise_ohlcv(raw)
        if result is None:
            return None
        if requested_end > today:
            result.attrs["requested_end"] = pd.Timestamp(requested_end, tz="UTC")
            result.attrs["history_end"] = pd.Timestamp(today, tz="UTC")
            result.attrs["future_data_unavailable"] = True
        if parsed:
            multiplier = response_multiplier or self._option_multiplier()
            if multiplier is None:
                print("[ThetaData] option contract multiplier is invalid; history rejected")
                return None
            result.attrs["option_symbol"] = normalized
            result.attrs["contract_multiplier"] = multiplier
            result.attrs["contract_multiplier_source"] = (
                "source" if response_multiplier is not None else "standard_market_assumption"
            )
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
                    greeks_kwargs.pop("start_date", None)
                    greeks_kwargs.pop("end_date", None)
                    greeks = self._history_or_chunk(
                        greeks_method,
                        start,
                        end,
                        _INTRADAY_CHUNK_DAYS,
                        **greeks_kwargs,
                    )
                else:
                    greeks_kwargs.pop("start_date", None)
                    greeks_kwargs.pop("end_date", None)
                    greeks = self._history_or_chunk(
                        greeks_method,
                        start,
                        end,
                        _EOD_RETRY_CHUNK_DAYS,
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
                open_interest = self._history_or_chunk(
                    "option_history_open_interest",
                    start,
                    end,
                    _EOD_RETRY_CHUNK_DAYS,
                    symbol=root,
                    expiration=expiry,
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
                result.attrs["contract_multiplier_source"] = (
                    "source" if response_multiplier is not None else "standard_market_assumption"
                )
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

    def _list_expirations(self, code):
        """列出并缓存某标的的到期日，避免每个 as_of 都打满 list 接口。"""
        with self._client_lock:
            cached = self._expiration_cache.get(code)
            if cached is not None:
                return list(cached)
        raw_expirations = self._call("option_list_expirations", symbol=code)
        exp_df = _to_pandas(raw_expirations)
        if exp_df is None or exp_df.empty:
            return []
        exp_col = _column(exp_df, ("expiration", "expiry", "date"))
        if exp_col is None:
            return []
        expiration_values = ThetaDataProvider._parse_timestamps(exp_df, exp_col)
        expirations = [value.date() for value in expiration_values.dropna().tolist()]
        with self._client_lock:
            self._expiration_cache.setdefault(code, expirations)
            return list(self._expiration_cache[code])

    @staticmethod
    def _spread_dates(dates, limit):
        """按时间顺序均匀抽样到期日，保证 DTE 窗口两端都有覆盖。"""
        items = [item for item in dates if item is not None]
        try:
            cap = int(limit)
        except (TypeError, ValueError, OverflowError):
            return items
        if cap <= 0 or len(items) <= cap:
            return items
        if cap == 1:
            return items[:1]
        chosen = []
        seen = set()
        last = len(items) - 1
        for index in range(cap):
            cursor = int(index * last / (cap - 1))
            while cursor in seen and cursor < last:
                cursor += 1
            if cursor in seen:
                continue
            seen.add(cursor)
            chosen.append(items[cursor])
        return chosen

    @staticmethod
    def _select_chain_expirations(expirations, as_of_date, min_dte, max_dte):
        """按 as_of 的 DTE 窗口挑选到期日；优先周五，避免日频到期日占满上限。"""
        eligible = []
        for expiry in sorted({value for value in expirations if value is not None}):
            dte = (expiry - as_of_date).days
            if dte < min_dte or dte > max_dte:
                continue
            eligible.append(expiry)
        fridays = [item for item in eligible if item.weekday() == 4]
        if fridays:
            if len(fridays) <= _MAX_AUTO_CHAIN_EXPIRATIONS:
                return fridays
            return ThetaDataProvider._spread_dates(fridays, _MAX_AUTO_CHAIN_EXPIRATIONS)
        if len(eligible) <= _MAX_AUTO_CHAIN_EXPIRATIONS:
            return eligible
        return ThetaDataProvider._spread_dates(eligible, _MAX_AUTO_CHAIN_EXPIRATIONS)

    def get_option_chain(
        self,
        underlying,
        expirations=None,
        normalized=False,
        as_of=None,
        start=None,
        end=None,
        min_dte=None,
        max_dte=None,
        strike_range=None,
        right=None,
    ):
        """获取 ThetaData 期权链；传入 as_of 时走当日 EOD，未传时才用当前快照。"""
        if (start is not None or end is not None) and as_of is None:
            print("[ThetaData] historical option-chain requests require as_of to prevent lookahead")
            return None
        raw_underlying = str(underlying or "").strip().upper()
        if "." in raw_underlying and not raw_underlying.startswith("US."):
            return None
        code = raw_underlying.removeprefix("US.")
        if not code:
            return None
        as_of_date = _as_date(as_of)
        if as_of is not None and as_of_date is None:
            return None
        # 当日 EOD 尚未完成；expiration=* 会被 Theta 拒绝，也不得回退当前快照冒充历史链。
        if as_of_date is not None and as_of_date >= _vendor_today():
            return None
        auto_expirations = expirations is None
        if auto_expirations:
            expirations = self._list_expirations(code)
            if not expirations:
                return None
        else:
            parsed_expirations = [_as_date(item) for item in expirations]
            expirations = [item for item in parsed_expirations if item is not None]
            if not expirations:
                return None
        right_value = _normalize_chain_right(right)
        min_dte_value = _bounded_int(min_dte, 0)
        if min_dte_value is None or min_dte_value < 0:
            min_dte_value = 0
        if max_dte is None:
            max_dte_value = _DEFAULT_AUTO_MAX_DTE if auto_expirations else None
        else:
            max_dte_value = _bounded_int(max_dte, _DEFAULT_AUTO_MAX_DTE if auto_expirations else None)
        if as_of_date is not None and (auto_expirations or min_dte is not None or max_dte is not None):
            high = _DEFAULT_AUTO_MAX_DTE if max_dte_value is None else max_dte_value
            expirations = self._select_chain_expirations(
                expirations,
                as_of_date,
                min_dte_value,
                high,
            )
            if not expirations:
                return None
        elif auto_expirations:
            cutoff = as_of_date or _vendor_today()
            future = sorted({value for value in expirations if value >= cutoff})
            expirations = future[:_MAX_AUTO_CHAIN_EXPIRATIONS] if future else expirations[:_MAX_AUTO_CHAIN_EXPIRATIONS]
        range_value = _bounded_int(strike_range)
        if range_value is None and auto_expirations and as_of_date is not None:
            range_value = _AUTO_CHAIN_STRIKE_RANGE
        if range_value is not None and range_value <= 0:
            range_value = None
        rows = []
        # ThetaData 支持 expiration='*'；历史链优先走一次批量 RPC，避免每个
        # as_of 对多个到期日逐个请求。显式 expirations 仍保留精确路径。
        if as_of_date is not None and auto_expirations:
            batch = self._get_historical_chain_batch(
                code,
                as_of_date,
                expirations,
                right_value,
                range_value,
            )
            if batch is not None:
                rows.append(batch)
                expirations = []

        for expiration in expirations:
            frame = None
            expiry = _as_date(expiration)
            if as_of_date is not None:
                # 历史链必须用当日 EOD，不能把当前 snapshot 伪装成 as_of。
                if expiry is None:
                    continue
                call_kwargs = {
                    "symbol": code,
                    "expiration": expiry,
                    "start_date": as_of_date,
                    "end_date": as_of_date,
                    "strike": "*",
                    "right": right_value,
                }
                if range_value is not None:
                    call_kwargs["strike_range"] = range_value
                for base_method in (
                    "option_history_greeks_eod",
                    "option_history_eod",
                ):
                    raw = self._call(
                        base_method,
                        **call_kwargs,
                    )
                    candidate = _to_pandas(raw)
                    if candidate is not None and not candidate.empty:
                        frame = candidate
                        break
                if frame is not None and not frame.empty:
                    frame = self._prepare_chain_frame(frame, code, expiry)
                    frame["timestamp"] = pd.Timestamp(as_of_date, tz="UTC")
                    rows.append(frame)
                continue
            # Greeks all 需要 Professional；Standard 订阅仍可使用一阶 Greeks，
            # 再由 OHLC/OI 快照补齐成交与未平仓量字段。
            if expiry is None:
                continue
            snap_kwargs = {
                "symbol": code,
                "expiration": expiry,
                "strike": "*",
                "right": right_value,
            }
            if range_value is not None:
                snap_kwargs["strike_range"] = range_value
            for base_method in (
                "option_snapshot_greeks_all",
                "option_snapshot_greeks_first_order",
                "option_snapshot_quote",
            ):
                raw = self._call(
                    base_method,
                    **snap_kwargs,
                )
                candidate = _to_pandas(raw)
                if candidate is not None and not candidate.empty:
                    frame = candidate
                    break
            if frame is not None and not frame.empty:
                frame = self._prepare_chain_frame(frame, code, expiry)
                for enrich_method in (
                    "option_snapshot_quote",
                    "option_snapshot_ohlc",
                    "option_snapshot_open_interest",
                ):
                    enrich_raw = self._call(
                        enrich_method,
                        **snap_kwargs,
                    )
                    enrich = _to_pandas(enrich_raw)
                    if enrich is not None and not enrich.empty:
                        frame = self._merge_chain_columns(
                            frame,
                            self._prepare_chain_frame(enrich, code, expiry),
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
        today = _vendor_today()
        start = _as_date(start_date) or expiry - _dt.timedelta(days=365)
        end = _as_date(end_date) or today
        if start > today:
            return None
        if end > today:
            end = today
        start, end = _clip_option_history_window(start, end, expiry)
        if start > end:
            return None
        normalized = str(timeframe or "").strip().lower()
        try:
            period = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            return None
        if method.endswith("_eod") or method == "option_history_open_interest":
            raw = self._history_or_chunk(
                method,
                start,
                end,
                _EOD_RETRY_CHUNK_DAYS,
                symbol=root,
                expiration=expiry,
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

    def _get_historical_chain_batch(
        self,
        code,
        as_of_date,
        expirations,
        right_value,
        range_value,
    ):
        """用一次 wildcard 历史请求取得一个 as_of 的整条链，再本地筛选到期日。"""
        if as_of_date >= _vendor_today():
            return None
        allowed = {pd.Timestamp(item).date() for item in expirations if item is not None}
        if not allowed:
            return None
        request = {
            "symbol": code,
            "expiration": "*",
            "start_date": as_of_date,
            "end_date": as_of_date,
            "strike": "*",
            "right": right_value,
        }
        if range_value is not None:
            request["strike_range"] = range_value

        frame = None
        for method in ("option_history_greeks_eod", "option_history_eod"):
            candidate = _to_pandas(self._call(method, **request))
            if candidate is not None and not candidate.empty:
                frame = candidate
                break
        if frame is None or frame.empty:
            return None

        prepared = self._prepare_chain_frame(frame, code, None)
        if "expiry" not in prepared.columns:
            return None
        if "option_symbol" not in prepared.columns:
            strike_col = _column(prepared, ("strike", "strike_price", "exercise_price"))
            right_col = _column(prepared, ("right", "option_type", "call_put", "cp", "type"))
            if strike_col is None or right_col is None:
                return None

            def make_symbol(row):
                try:
                    expiry = _as_date(row.get("expiry"))
                    strike = float(row[strike_col])
                    right = str(row[right_col]).strip().upper()
                    right = "C" if right in {"C", "CALL", "1"} else "P" if right in {"P", "PUT", "2"} else ""
                    if expiry is None or not right or not math.isfinite(strike) or strike <= 0:
                        return None
                    return f"US.{code}{expiry.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"
                except (TypeError, ValueError, OverflowError):
                    return None

            prepared["option_symbol"] = prepared.apply(make_symbol, axis=1)
        expiry_values = pd.to_datetime(prepared["expiry"], errors="coerce", utc=True)
        prepared = prepared.loc[expiry_values.dt.date.isin(allowed)].copy()
        if prepared.empty:
            return None
        prepared["timestamp"] = pd.Timestamp(as_of_date, tz="UTC")
        return prepared

    def close(self):
        """关闭 ThetaData 客户端（若 SDK 提供 close）。"""
        with self._client_lock:
            self._client_stale = False
            client, self.client = self.client, None
            if client is None:
                return
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


__all__ = ["ThetaDataProvider"]
