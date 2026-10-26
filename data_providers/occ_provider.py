"""OCC 调整合约与公司行动 Provider。

OCC 调整文件的格式会随发布渠道变化，因此 Provider 支持官方 CSV/JSON 下载和
本地归档两种输入。没有可验证的调整记录时不凭空修改标准合约乘数。
"""

from __future__ import annotations

import json
import time
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

import config
from common.data_paths import ensure_data_dir, resolve_data_path, share_data_file
from common.options.analytics import parse_option_symbol, underlying_key
from .base_provider import BaseDataProvider


class OccDataProvider(BaseDataProvider):
    """读取 OCC 调整合约归档，提供点时乘数、执行价和交割物信息。"""

    PRIORITY = 94
    AUXILIARY_ONLY = True
    OPTION_ADJUSTMENTS = True
    REQUEST_TIMEOUT_SECONDS = 20
    CACHE_TTL_SECONDS = 24 * 60 * 60

    _COLUMNS = [
        "option_symbol", "underlying", "effective_date", "announcement_date",
        "adjustment_type", "old_strike", "new_strike", "old_multiplier",
        "new_multiplier", "old_underlying", "new_underlying", "cash_component",
        "deliverable", "source_id", "standard_contract",
    ]

    def __init__(self):
        root = Path(resolve_data_path(getattr(config, "DATA_PATH", ".data")))
        self.cache_dir = Path(ensure_data_dir(root / "market_cache" / "occ"))
        self.path = str(getattr(config, "OCC_ACTIONS_PATH", "") or "").strip()
        self.url = str(getattr(config, "OCC_ACTIONS_URL", "") or "").strip()
        self._memory = None

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        """OCC 是合约辅助源，不提供 OHLCV。"""
        return None

    def get_contract_adjustments(
        self, symbols=None, start_date=None, end_date=None, as_of=None,
    ) -> pd.DataFrame | None:
        """返回指定合约或底层的调整事件，日期均为生效日。"""
        frame = self._load_records()
        if frame is None:
            return None
        result = frame.copy()
        allowed = {str(item or "").strip().upper() for item in (symbols or []) if str(item).strip()}
        if allowed:
            keys = set(allowed)
            keys.update(underlying_key(item) for item in allowed)
            result = result.loc[
                result["option_symbol"].astype("string").str.upper().isin(keys)
                | result["underlying"].astype("string").str.upper().map(underlying_key).isin(keys)
            ]
        start = _date(start_date)
        end = _date(end_date)
        cutoff = _date(as_of)
        if start is not None:
            result = result.loc[result["effective_date"] >= start]
        if end is not None:
            result = result.loc[result["effective_date"] <= end]
        if cutoff is not None:
            result = result.loc[
                result["effective_date"].le(cutoff)
                & (
                    result["announcement_date"].isna()
                    | result["announcement_date"].le(cutoff)
                )
            ]
        result = result.reset_index(drop=True)
        result.attrs.update(getattr(frame, "attrs", {}) or {})
        return result

    def get_option_contract_metadata(self, symbol, as_of=None):
        """返回某一 OCC 合约的最新已生效调整；未知时明确返回 unknown。"""
        parsed = parse_option_symbol(symbol)
        if not parsed.get("option_type"):
            return {"status": "not_option", "option_symbol": str(symbol or "").upper()}
        frame = self.get_contract_adjustments(
            symbols=[str(symbol or "").upper(), parsed.get("underlying", "")], as_of=as_of,
        )
        if frame is None:
            return {
                "status": "unknown", "option_symbol": str(symbol or "").upper(),
                "coverage_complete": False,
            }
        exact = frame.loc[
            frame["option_symbol"].astype("string").str.upper().eq(str(symbol or "").upper())
        ]
        selected = exact if not exact.empty else frame
        if selected.empty:
            return {
                "status": "not_adjusted", "option_symbol": str(symbol or "").upper(),
                "coverage_complete": bool(frame.attrs.get("coverage_complete", False)),
            }
        row = selected.sort_values("effective_date").iloc[-1]
        result = {str(key): value for key, value in row.to_dict().items()}
        result["status"] = "adjusted"
        result["coverage_complete"] = bool(frame.attrs.get("coverage_complete", False))
        return result

    def _load_records(self):
        if self._memory is not None:
            return self._memory.copy()
        raw = None
        source = ""
        path = Path(resolve_data_path(self.path)) if self.path and not Path(self.path).is_absolute() else (
            Path(self.path) if self.path else None
        )
        if path is not None and path.is_file():
            raw = _read_payload(path)
            source = "occ_local_archive"
        elif self.url:
            cache_path = self.cache_dir / "adjustments.json"
            if cache_path.is_file() and time.time() - cache_path.stat().st_mtime <= self.CACHE_TTL_SECONDS:
                raw = _read_payload(cache_path)
                source = "occ_cached_download"
            else:
                raw = self._download(self.url)
                if raw is not None:
                    try:
                        cache_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                        share_data_file(cache_path)
                    except OSError:
                        pass
                source = "occ_official_download"
        if raw is None:
            return None
        frame = _normalize(raw)
        frame.attrs["source"] = source
        frame.attrs["coverage_complete"] = bool(frame.attrs.get("coverage_complete", False))
        self._memory = frame.copy()
        return frame

    def _download(self, url):
        request = Request(url, headers={"User-Agent": "QuantAda/1.0 OCC contract research"})
        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:
                payload = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"[OCC] adjustment feed request failed: {exc}")
            return None
        try:
            if str(response.headers.get("content-type", "")).lower().find("json") >= 0:
                return json.loads(payload.decode("utf-8"))
            return pd.read_csv(BytesIO(payload)).to_dict("records")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"[OCC] adjustment feed parse failed: {exc}")
            return None


def _read_payload(path):
    try:
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path).to_dict("records")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _normalize(raw):
    if isinstance(raw, dict):
        raw = raw.get("records", raw.get("data", raw.get("adjustments", [])))
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return pd.DataFrame(columns=OccDataProvider._COLUMNS)
    frame = pd.DataFrame(raw)
    columns = {str(column).strip().lower(): column for column in frame.columns}

    def column(*names):
        for name in names:
            if name.lower() in columns:
                return frame[columns[name.lower()]]
        return pd.Series([None] * len(frame), index=frame.index)

    result = pd.DataFrame({
        "option_symbol": column("option_symbol", "contract_symbol", "occ_symbol", "symbol").astype("string").str.upper(),
        "underlying": column("underlying", "underlying_symbol", "root").astype("string").str.upper(),
        "effective_date": column("effective_date", "adjustment_date", "ex_date", "date").map(_date),
        "announcement_date": column("announcement_date", "announce_date", "published_date").map(_date),
        "adjustment_type": column("adjustment_type", "action_type", "type").astype("string"),
        "old_strike": pd.to_numeric(column("old_strike", "strike", "original_strike"), errors="coerce"),
        "new_strike": pd.to_numeric(column("new_strike", "adjusted_strike"), errors="coerce"),
        "old_multiplier": pd.to_numeric(column("old_multiplier", "multiplier", "original_multiplier"), errors="coerce"),
        "new_multiplier": pd.to_numeric(column("new_multiplier", "adjusted_multiplier"), errors="coerce"),
        "old_underlying": column("old_underlying", "original_underlying").astype("string"),
        "new_underlying": column("new_underlying", "adjusted_underlying").astype("string"),
        "cash_component": pd.to_numeric(column("cash_component", "cash", "cash_amount"), errors="coerce"),
        "deliverable": column("deliverable", "deliverable_description").astype("string"),
        "source_id": column("source_id", "memo_id", "memo", "id").astype("string"),
        "standard_contract": column("standard_contract", "is_standard").map(_bool_or_na),
    })
    result = result.loc[result["effective_date"].notna()].reset_index(drop=True)
    result.attrs["coverage_complete"] = bool(len(result) > 0 and result["source_id"].astype("string").str.len().gt(0).all())
    return result


def _date(value):
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).normalize()


def _bool_or_na(value):
    if pd.isna(value):
        return pd.NA
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


__all__ = ["OccDataProvider"]
