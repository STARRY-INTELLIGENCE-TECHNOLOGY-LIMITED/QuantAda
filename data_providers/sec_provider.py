"""SEC companyfacts 基本面估值快照 Provider。"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

import config
from common.data_paths import ensure_data_dir, resolve_data_path, share_data_file
from common.fundamentals import FUNDAMENTAL_FIELDS
from common.options.valuation import compute_fcfe_valuation_snapshots
from .base_provider import BaseDataProvider, REQUEST_ATTEMPTS


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_REQUEST_TIMEOUT_SECONDS = 15
SEC_CACHE_TTL_SECONDS = 24 * 60 * 60
SEC_VALUATION_MAX_AGE_DAYS = 200
FCFE_DISCOUNT_RATE = 0.10
FCFE_TERMINAL_GROWTH = 0.02
FCFE_HISTORY_PERIODS = 3
_ANNUAL_FORM_TYPES = {"10-K", "10-K/A", "20-F", "20-F/A"}
_QUARTERLY_FORM_TYPES = {"10-Q", "10-Q/A"}
_ANNUAL_FLOW_TAGS = {
    "fcf": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
        "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssetsAndOtherProductiveAssets",
    ),
    "shares": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
    ),
}
_FUNDAMENTAL_FLOW_TAGS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet", "Revenues", "SalesRevenueGoodsNet",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "stock_based_compensation": (
        "ShareBasedCompensation", "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsGrantsInPeriod",
    ),
    "buybacks": (
        "PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfCommonStocks",
    ),
}
_FUNDAMENTAL_INSTANT_TAGS = {
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "debt": (
        "LongTermDebtNoncurrent", "LongTermDebtCurrent", "LongTermDebt",
        "ShortTermBorrowings",
    ),
}
_CACHE_LOCK = threading.RLock()
_REQUEST_LOCK = threading.Lock()
_REQUEST_MIN_INTERVAL_SECONDS = 0.12
_LAST_REQUEST_AT = 0.0


class SecDataProvider(BaseDataProvider):
    """为美国 SEC 报告主体生成带 filing 日期的 FCFE 估值序列。"""

    PRIORITY = 95
    VALUATION_ONLY = True
    VALUATION_MAX_AGE_DAYS = SEC_VALUATION_MAX_AGE_DAYS

    def __init__(self):
        self.user_agent = str(getattr(config, "SEC_USER_AGENT", "") or "").strip()
        self.cache_dir = Path(resolve_data_path(getattr(config, "DATA_PATH", ".data"))) / "market_cache" / "sec"
        self._memory_cache: dict[str, tuple[float, object]] = {}
        self._warned_missing_user_agent = False

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        """SEC 是估值补充源，不提供 OHLCV 行情。"""
        return None

    def get_earnings_calendar(self, symbols=None, start_date=None, end_date=None, as_of=None):
        """用已提交财报的 filing 日期构造无前视的实际公告事件。"""
        raw_symbols = [symbols] if isinstance(symbols, str) else list(symbols or [])
        rows = []
        for item in raw_symbols:
            ticker = normalize_us_ticker(item)
            if ticker is None:
                continue
            ticker_map = self._load_json("company_tickers", SEC_TICKERS_URL)
            cik = _find_cik(ticker_map, ticker) if isinstance(ticker_map, dict) else None
            if cik is None:
                continue
            facts = self._load_json(
                f"companyfacts_{cik:010d}",
                SEC_COMPANYFACTS_URL.format(cik=f"{cik:010d}"),
            )
            if not isinstance(facts, dict):
                continue
            reports = point_in_time_fcfe_rows_from_sec_facts(facts)
            for row in reports.to_dict("records"):
                filing = pd.Timestamp(row["asof"]).normalize()
                rows.append({
                    "symbol": f"US.{ticker}",
                    "event_date": filing,
                    "visible_from": filing,
                    "event_type": "filing",
                    "event_role": "filing",
                    "period_text": str(row.get("report_end", "")),
                    "source": "sec_filing_date",
                })
        if not rows:
            return None
        result = pd.DataFrame(rows)
        if as_of:
            cutoff = pd.Timestamp(as_of).normalize()
            result = result.loc[result["visible_from"] < cutoff].reset_index(drop=True)
            if result.empty:
                return None
        result.attrs["source"] = "sec_filing_date"
        result.attrs["as_of_supported"] = True
        return result

    def get_valuation_snapshots(self, symbol, start_date=None, end_date=None) -> pd.DataFrame | None:
        """读取 SEC 财报事实并返回 point-in-time 每股公允价值快照。"""
        ticker = normalize_us_ticker(symbol)
        if ticker is None:
            return _empty_snapshots()
        ticker_map = self._load_json("company_tickers", SEC_TICKERS_URL)
        if not isinstance(ticker_map, dict):
            return None
        cik = _find_cik(ticker_map, ticker)
        if cik is None:
            return _empty_snapshots()
        facts = self._load_json(f"companyfacts_{cik:010d}", SEC_COMPANYFACTS_URL.format(cik=f"{cik:010d}"))
        if not isinstance(facts, dict):
            return None
        fcfe_reports = point_in_time_fcfe_rows_from_sec_facts(facts)
        if fcfe_reports.empty:
            return _empty_snapshots()
        try:
            result = compute_fcfe_valuation_snapshots(
                fcfe_reports,
                f"US.{ticker}",
                discount_rate=FCFE_DISCOUNT_RATE,
                terminal_growth=FCFE_TERMINAL_GROWTH,
                history_periods=FCFE_HISTORY_PERIODS,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            print(f"[SEC] Valuation calculation failed for US.{ticker}: {exc}")
            return _empty_snapshots()
        if end_date:
            result = result.loc[result["asof"] <= pd.Timestamp(end_date).normalize()]
        result.attrs["valuation_source"] = "sec_companyfacts"
        result.attrs["valuation_method"] = "sec_fcfe_perpetuity_v1"
        return result.reset_index(drop=True)

    def get_fundamental_snapshots(self, symbol, start_date=None, end_date=None):
        """返回 filing-date 对齐的收入、利润、SBC、现金、债务和股本快照。"""
        ticker = normalize_us_ticker(symbol)
        if ticker is None:
            return pd.DataFrame()
        ticker_map = self._load_json("company_tickers", SEC_TICKERS_URL)
        cik = _find_cik(ticker_map, ticker) if isinstance(ticker_map, dict) else None
        if cik is None:
            return pd.DataFrame()
        facts = self._load_json(
            f"companyfacts_{cik:010d}",
            SEC_COMPANYFACTS_URL.format(cik=f"{cik:010d}"),
        )
        if not isinstance(facts, dict):
            return pd.DataFrame()
        rows = point_in_time_fundamental_rows_from_sec_facts(facts)
        if rows.empty:
            return rows
        rows["ticker"] = f"US.{ticker}"
        if start_date:
            rows = rows.loc[rows["asof"] >= pd.Timestamp(start_date).normalize()]
        if end_date:
            rows = rows.loc[rows["asof"] <= pd.Timestamp(end_date).normalize()]
        rows.attrs["source"] = "sec_companyfacts_fundamentals"
        return rows.reset_index(drop=True)

    def _load_json(self, cache_name: str, url: str):
        """按本地缓存优先读取 JSON，并有界刷新过期内容。"""
        cache_path = self.cache_dir / f"{cache_name}.json"
        with _CACHE_LOCK:
            memory = self._memory_cache.get(cache_name)
            if memory is not None and time.time() - memory[0] < SEC_CACHE_TTL_SECONDS:
                return memory[1]
            cached = _read_cache(cache_path)
            if cached is not None and time.time() - cached[0] < SEC_CACHE_TTL_SECONDS:
                self._memory_cache[cache_name] = cached
                return cached[1]
            if not self.user_agent:
                if not self._warned_missing_user_agent:
                    print(
                        "[SEC] SEC_USER_AGENT must identify the application and a contact email; "
                        "online fetch skipped."
                    )
                    self._warned_missing_user_agent = True
                return None if cached is None else cached[1]
            payload = _download_json(url, self.user_agent)
            if payload is None:
                if cached is not None:
                    print(f"[SEC] Using stale cached JSON after request failure: {cache_name}.")
                    return cached[1]
                return None
            try:
                _write_cache(cache_path, payload)
            except OSError as exc:
                print(f"[SEC] Cache write failed for {cache_name}: {exc}")
            item = (time.time(), payload)
            self._memory_cache[cache_name] = item
            return payload


def normalize_us_ticker(symbol) -> str | None:
    """只接受普通美股代码；期权和非美市场返回空值。"""
    raw = str(symbol or "").strip().upper()
    if raw.startswith("US."):
        raw = raw[3:]
    elif "." in raw or ":" in raw:
        return None
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", raw):
        return None
    return raw


def point_in_time_fcfe_rows_from_sec_facts(companyfacts: dict) -> pd.DataFrame:
    """从 10-K/10-Q 事实重建逐次披露的年度或 TTM FCFE。"""
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    cfo_rows = _period_fact_rows(facts, _ANNUAL_FLOW_TAGS["fcf"], {"USD"})
    capex_rows = _period_fact_rows(facts, _ANNUAL_FLOW_TAGS["capex"], {"USD"})
    share_rows = _period_fact_rows(facts, _ANNUAL_FLOW_TAGS["shares"], {"shares"})
    instant_shares = _instant_fact_rows(companyfacts.get("facts", {}))
    if not cfo_rows or not capex_rows:
        return pd.DataFrame(columns=["asof", "report_end", "fcfe", "shares", "source_id"])

    annual_cfo = [row for row in cfo_rows if row["form"] in _ANNUAL_FORM_TYPES]
    annual_capex = [row for row in capex_rows if row["form"] in _ANNUAL_FORM_TYPES]
    quarter_cfo = [row for row in cfo_rows if row["form"] in _QUARTERLY_FORM_TYPES]
    quarter_capex = [row for row in capex_rows if row["form"] in _QUARTERLY_FORM_TYPES]
    events = _filing_events(cfo_rows)
    output = []
    for event in events:
        filed = event["filed"]
        if event["form"] in _ANNUAL_FORM_TYPES:
            capex = _match_period_fact(annual_capex, event)
            shares = _match_period_fact(share_rows, event)
            if capex is None:
                continue
            fcfe = event["value"] - abs(capex["value"])
        else:
            current_capex = _match_period_fact(quarter_capex, event)
            previous_annual_cfo = _latest_annual_before(annual_cfo, event["start"], filed)
            previous_annual_capex = (
                _match_period_fact(annual_capex, previous_annual_cfo)
                if previous_annual_cfo is not None else None
            )
            previous_ytd_cfo = _same_period_last_year(quarter_cfo, event, filed)
            previous_ytd_capex = (
                _match_period_fact(quarter_capex, previous_ytd_cfo)
                if previous_ytd_cfo is not None else None
            )
            if any(item is None for item in (
                current_capex, previous_annual_cfo, previous_annual_capex,
                previous_ytd_cfo, previous_ytd_capex,
            )):
                continue
            ttm_cfo = previous_annual_cfo["value"] + event["value"] - previous_ytd_cfo["value"]
            ttm_capex = (
                previous_annual_capex["value"]
                + current_capex["value"]
                - previous_ytd_capex["value"]
            )
            fcfe = ttm_cfo - abs(ttm_capex)
            shares = _match_quarter_share(share_rows, event, filed)

        if shares is None:
                shares = _latest_instant_share(instant_shares, filed_by=filed, end_by=event["end"])
        if shares is None or shares["value"] <= 0:
            continue
        accession = event.get("accn") or ""
        output.append(
            {
                "asof": filed,
                "report_end": event["end"],
                "fcfe": fcfe,
                "shares": shares["value"],
                "source_id": f"sec:{accession}" if accession else f"sec:{event['end']}:{filed}",
            }
        )
    return pd.DataFrame(output, columns=["asof", "report_end", "fcfe", "shares", "source_id"])


def point_in_time_fundamental_rows_from_sec_facts(companyfacts: dict) -> pd.DataFrame:
    """按 filing 日期抽取标准 US-GAAP 经营和资本结构字段。"""
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    all_flow_rows = {}
    for field, tags in _FUNDAMENTAL_FLOW_TAGS.items():
        all_flow_rows[field] = _period_fact_rows(facts, tags, {"USD"})
    cfo_rows = _period_fact_rows(facts, _ANNUAL_FLOW_TAGS["fcf"], {"USD"})
    share_rows = _period_fact_rows(facts, _ANNUAL_FLOW_TAGS["shares"], {"shares"})
    event_rows = cfo_rows
    if not event_rows:
        event_rows = all_flow_rows.get("revenue", [])
    instant_rows = {
        field: _instant_rows_for_tags(facts, tags, {"USD"})
        for field, tags in _FUNDAMENTAL_INSTANT_TAGS.items()
    }
    output = []
    for event in _filing_events(event_rows):
        record = {
            "asof": pd.Timestamp(event["filed"]),
            "report_end": pd.Timestamp(event["end"]),
            "source_id": f"sec:{event.get('accn') or event['end']}:{event['filed']}",
        }
        for field, rows in all_flow_rows.items():
            matched = _match_period_fact(rows, event)
            record[field] = None if matched is None else matched["value"]
        shares = _match_period_fact(share_rows, event)
        record["shares"] = None if shares is None else shares["value"]
        for field, rows in instant_rows.items():
            if field == "debt":
                record[field] = _latest_fact_sum(rows, end=event["end"], filed_by=event["filed"])
            else:
                matched = _latest_fact(rows, end=event["end"], filed_by=event["filed"])
                record[field] = None if matched is None else matched["value"]
        output.append(record)
    if not output:
        return pd.DataFrame(columns=["ticker", "asof", "report_end", *FUNDAMENTAL_FIELDS, "source_id"])
    result = pd.DataFrame(output)
    result["debt"] = pd.to_numeric(result.get("debt"), errors="coerce")
    return result[["asof", "report_end", *FUNDAMENTAL_FIELDS, "source_id"]]


def _period_fact_rows(facts, tag_names, units):
    """按首个有覆盖的标准 US-GAAP 标签读取年度和季度事实。"""
    for tag in tag_names:
        concept = facts.get(tag)
        if not isinstance(concept, dict):
            continue
        unit_rows = concept.get("units", {})
        selected = next((unit_rows.get(unit) for unit in units if unit in unit_rows), None)
        if not isinstance(selected, list):
            continue
        rows = []
        for item in selected:
            form = str(item.get("form", "")).upper()
            fp = str(item.get("fp", "")).upper()
            annual = form in _ANNUAL_FORM_TYPES and fp == "FY"
            quarterly = form in _QUARTERLY_FORM_TYPES and fp in {"Q1", "Q2", "Q3"}
            if not annual and not quarterly:
                continue
            start = _date(item.get("start"))
            end = _date(item.get("end"))
            filed = _date(item.get("filed"))
            if start is None or end is None or filed is None:
                continue
            span = (end - start).days
            if annual and not 330 <= span <= 400:
                continue
            if quarterly and not 60 <= span <= 300:
                continue
            value = _finite(item.get("val"))
            if value is None:
                continue
            rows.append({
                "start": start,
                "end": end,
                "filed": filed,
                "value": value,
                "accn": str(item.get("accn", "")),
                "form": form,
                "fp": fp,
                "fy": item.get("fy"),
                "span": span,
            })
        if rows:
            return rows
    return []


def _instant_rows_for_tags(facts, tag_names, units):
    """读取期末事实；多个债务标签由调用方在同一 filing 记录中累加。"""
    rows = []
    for tag in tag_names:
        concept = facts.get(tag)
        if not isinstance(concept, dict):
            continue
        selected = None
        for unit in units:
            if unit in concept.get("units", {}):
                selected = concept["units"][unit]
                break
        if not isinstance(selected, list):
            continue
        for item in selected:
            form = str(item.get("form", "")).upper()
            if form not in (_ANNUAL_FORM_TYPES | _QUARTERLY_FORM_TYPES):
                continue
            end, filed = _date(item.get("end")), _date(item.get("filed"))
            value = _finite(item.get("val"))
            if end is not None and filed is not None and value is not None:
                rows.append({"end": end, "filed": filed, "value": value, "tag": tag})
        if rows:
            # 保留所有标签，现金/债务字段在同一 filing 内可累加。
            continue
    return rows


def _filing_events(cfo_rows):
    """每份 filing 对每个报告期保留最长现金流累计区间。"""
    groups = {}
    for row in cfo_rows:
        key = (row["filed"], row["end"], row["form"], row["fp"])
        old = groups.get(key)
        if old is None or row["span"] > old["span"]:
            groups[key] = row
    return sorted(groups.values(), key=lambda row: (row["filed"], row["end"]))


def _match_period_fact(rows, event):
    """匹配同一 end/start 的现金流或股数事实。"""
    candidates = [
        row for row in rows
        if row["end"] == event["end"] and row["filed"] <= event["filed"]
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            row["start"] == event["start"],
            row["filed"],
            -abs(row["span"] - event["span"]),
        ),
    )


def _latest_annual_before(rows, start, filed_by):
    """读取季度累计期开始之前最近的完整财年。"""
    candidates = [
        row for row in rows
        if row["fp"] == "FY" and row["end"] < start and row["filed"] <= filed_by
    ]
    if not candidates:
        return None
    latest_end = max(row["end"] for row in candidates)
    same_end = [row for row in candidates if row["end"] == latest_end]
    return max(same_end, key=lambda row: row["filed"])


def _same_period_last_year(rows, event, filed_by):
    """匹配上一财年相同季度的累计现金流。"""
    target_start = _same_day_previous_year(event["start"])
    target_end = _same_day_previous_year(event["end"])
    candidates = [
        row for row in rows
        if row["fp"] == event["fp"]
        and row["filed"] <= filed_by
        and abs((row["start"] - target_start).days) <= 8
        and abs((row["end"] - target_end).days) <= 8
    ]
    return max(candidates, key=lambda row: (row["filed"], row["span"])) if candidates else None


def _same_day_previous_year(value: date) -> date:
    """返回上一年的同一日，闰日按二月末收敛。"""
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _match_quarter_share(rows, event, filed_by):
    """选择当季稀释加权股数，缺失时由调用方回退期末股数。"""
    candidates = [
        row for row in rows
        if row["end"] == event["end"]
        and row["filed"] <= filed_by
        and row["form"] in _QUARTERLY_FORM_TYPES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row["filed"], -abs(row["span"] - 90)))


def _instant_fact_rows(facts):
    """读取公司披露的期末普通股流通股数。"""
    result = []
    for namespace in facts.values() if isinstance(facts, dict) else ():
        if not isinstance(namespace, dict):
            continue
        concept = namespace.get("EntityCommonStockSharesOutstanding")
        if not isinstance(concept, dict):
            continue
        for unit, rows in concept.get("units", {}).items():
            if unit.lower() not in {"shares", "share"} or not isinstance(rows, list):
                continue
            for item in rows:
                if str(item.get("form", "")).upper() not in (_ANNUAL_FORM_TYPES | _QUARTERLY_FORM_TYPES):
                    continue
                end, filed = _date(item.get("end")), _date(item.get("filed"))
                value = _finite(item.get("val"))
                if end is not None and filed is not None and value is not None:
                    result.append({"end": end, "filed": filed, "value": value})
    return result


def _latest_fact(rows, *, end, filed_by):
    candidates = [row for row in rows if row["end"] == end and row["filed"] <= filed_by]
    return max(candidates, key=lambda row: row["filed"]) if candidates else None


def _latest_fact_sum(rows, *, end, filed_by):
    """按同一 filing 的不同债务标签求和，避免只取长期债务一段。"""
    candidates = [row for row in rows if row["end"] == end and row["filed"] <= filed_by]
    if not candidates:
        return None
    latest = max(row["filed"] for row in candidates)
    values = [row["value"] for row in candidates if row["filed"] == latest]
    return float(sum(values)) if values else None


def _latest_instant_share(rows, *, filed_by, end_by):
    candidates = [row for row in rows if row["filed"] <= filed_by and row["end"] <= end_by]
    return max(candidates, key=lambda row: (row["end"], row["filed"])) if candidates else None


def _find_cik(ticker_map, ticker):
    wanted = {ticker.upper(), ticker.upper().replace(".", "-")}
    for item in ticker_map.values():
        if not isinstance(item, dict) or str(item.get("ticker", "")).upper() not in wanted:
            continue
        try:
            cik = int(item["cik_str"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return cik if cik > 0 else None
    return None


def _date(value):
    if value is None:
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.tz_localize(None)
    return parsed.date()


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _empty_snapshots():
    return pd.DataFrame(columns=["ticker", "asof", "v", "w", "confidence", "method", "source_id"])


def _read_cache(path: Path):
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(content["fetched_at"])
        payload = content["payload"]
        return fetched_at, payload
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_cache(path: Path, payload) -> None:
    ensure_data_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"fetched_at": time.time(), "payload": payload}, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    share_data_file(path)


def _download_json(url: str, user_agent: str):
    global _LAST_REQUEST_AT
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        request = Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with _REQUEST_LOCK:
                elapsed = time.monotonic() - _LAST_REQUEST_AT
                if elapsed < _REQUEST_MIN_INTERVAL_SECONDS:
                    time.sleep(_REQUEST_MIN_INTERVAL_SECONDS - elapsed)
                _LAST_REQUEST_AT = time.monotonic()
            with urlopen(request, timeout=SEC_REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {400, 401, 403, 404}:
                print(f"[SEC] Request rejected ({exc.code}) for {url}.")
                return None
            error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            error = str(exc)
        if attempt < REQUEST_ATTEMPTS:
            time.sleep(min(0.25 * attempt, 1.0))
    print(f"[SEC] Request failed after {REQUEST_ATTEMPTS} attempts for {url}: {error}")
    return None


__all__ = [
    "FCFE_DISCOUNT_RATE",
    "FCFE_HISTORY_PERIODS",
    "FCFE_TERMINAL_GROWTH",
    "SEC_VALUATION_MAX_AGE_DAYS",
    "SecDataProvider",
    "normalize_us_ticker",
    "point_in_time_fcfe_rows_from_sec_facts",
]
