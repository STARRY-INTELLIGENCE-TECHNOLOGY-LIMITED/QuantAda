"""Covered Call 样例：必须已有足够正股，才能卖开 Call。

复制执行。密钥只放环境变量 FUTU_UNLOCK_PASSWORD，不要写进本文件。
`--params` 与类默认参数一致；没有足够正股会被策略拦住。平仓调试把 defense_dte 调大。

开仓:
python run.py strategies.options.sample_covered_call_strategy.SampleCoveredCallStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': 0.10, 'max_delta': 0.30, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'profit_take_fraction': 0.50, 'defense_dte': 21, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"

平仓:
python run.py strategies.options.sample_covered_call_strategy.SampleCoveredCallStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': 0.10, 'max_delta': 0.30, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'profit_take_fraction': 0.50, 'defense_dte': 60, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"
"""

from __future__ import annotations

from common.options.analytics import safe_number
from common.options.risk import OptionRiskLeg
from common.data_view import (
    bar_datetime,
    pending_symbols,
    position_price,
    position_size,
    require_close_column,
)
from strategies.base_strategy import BaseStrategy
from strategies.options.support import (
    dte_days,
    iter_option_rows,
    matches_chain_window,
    option_limit_price,
    reserved_underlying_keys,
    underlying_feed,
)


class SampleCoveredCallStrategy(BaseStrategy):
    """用已持有正股覆盖短 Call；没有足够标的时不开仓。"""

    option_universe = ("CALL",)
    params = {
        "min_dte": 30,
        "max_dte": 45,
        "min_delta": 0.10,
        "max_delta": 0.30,
        "min_iv_percentile": 0.0,
        "max_spread_pct": 0.25,
        "min_open_interest": 0.0,
        "contracts": 1,
        "profit_take_fraction": 0.50,
        "defense_dte": 21,
        "rebalance_when": "daily",
    }

    def __init__(self, broker, params=None):
        super().__init__(broker, params)
        self.last_signals = []

    def init(self):
        require_close_column(self.broker)

    def _submit(self, data, volume, effect, price, **kwargs):
        submitter = getattr(self.broker, "submit_option_order", None)
        if not callable(submitter):
            return None
        return submitter(
            data,
            volume,
            effect,
            price=price if price and price > 0 else None,
            **kwargs,
        )

    def next(self):
        self.last_signals = []
        datas = list(getattr(self.broker, "datas", []) or [])
        if not datas:
            return
        current_dt = bar_datetime(datas[0], self.broker)
        if current_dt is None:
            return
        pending = pending_symbols(self.broker)
        candidates = []
        occupied = reserved_underlying_keys(self.broker, {"CALL"})

        for data, _row, meta, quote in iter_option_rows(self.broker, current_dt, {"CALL"}):
            symbol = meta["symbol"]
            if symbol.upper() in pending:
                occupied.add(meta["underlying_key"])
                continue
            size = position_size(self.broker, data)
            if size < 0:
                occupied.add(meta["underlying_key"])
                dte = dte_days(meta["expiry"], current_dt)
                entry = position_price(self.broker, data)
                target = entry * (1.0 - float(self.p.profit_take_fraction)) if entry > 0 else 0.0
                should_close = (dte is not None and dte <= int(self.p.defense_dte)) or (
                    target > 0 and quote["ask"] > 0 and quote["ask"] <= target
                )
                signal = {
                    "symbol": symbol,
                    "action": "BUY_TO_CLOSE" if should_close else "HOLD_SHORT",
                    "contracts": abs(size),
                    "dte": dte,
                }
                if should_close:
                    order = self._submit(
                        data, abs(size), "BUY_TO_CLOSE",
                        option_limit_price(self.broker, data, quote, "BUY_TO_CLOSE"),
                    )
                    signal["submitted"] = order is not None
                self.last_signals.append(signal)
                continue
            if size != 0:
                occupied.add(meta["underlying_key"])
                continue
            if not matches_chain_window(meta, quote, current_dt, self.p):
                continue
            target_delta = (float(self.p.min_delta) + float(self.p.max_delta)) / 2.0
            candidates.append({
                "data": data,
                "meta": meta,
                "quote": quote,
                "score": (abs(quote["delta"] - target_delta), quote["spread_pct"], -quote["open_interest"], symbol),
            })

        print(
            "[SampleCoveredCallStrategy] occupied=%s candidates=%s signals=%s"
            % (sorted(occupied), [item["meta"]["symbol"] for item in candidates], self.last_signals)
        )
        if not candidates:
            return
        if not self.should_execute_rebalance(
            target_symbols=[item["data"] for item in candidates],
            rebalance_when=self.p.rebalance_when,
        ):
            return

        for item in sorted(candidates, key=lambda value: value["score"]):
            key = item["meta"]["underlying_key"]
            if key in occupied:
                continue
            underlying = underlying_feed(self.broker, key)
            if underlying is None:
                self.last_signals.append({
                    "symbol": item["meta"]["symbol"],
                    "action": "SELL_TO_OPEN",
                    "submitted": False,
                    "blocked_reason": "underlying_unavailable",
                })
                continue
            multiplier = safe_number(self.broker.get_contract_multiplier(item["data"]), 0.0)
            shares = position_size(self.broker, underlying)
            required = float(self.p.contracts) * multiplier
            if multiplier <= 0 or shares < required:
                signal = {
                    "symbol": item["meta"]["symbol"],
                    "action": "SELL_TO_OPEN",
                    "submitted": False,
                    "blocked_reason": "covered_call_shares_insufficient",
                    "underlying_size": shares,
                    "required_shares": required,
                }
                self.last_signals.append(signal)
                print("[SampleCoveredCallStrategy] %s" % signal)
                continue
            spot = safe_number(self.broker.get_current_price(underlying), 0.0)
            if spot <= 0:
                continue
            risk_leg = OptionRiskLeg(
                item["meta"]["symbol"],
                getattr(underlying, "_name", key),
                "CALL",
                -float(self.p.contracts),
                item["meta"]["strike"],
                item["quote"]["bid"],
                spot,
                multiplier,
                expiry=item["meta"]["expiry"],
                historical_volatility=item["quote"]["historical_volatility"],
                price_shock=0.20,
            )
            price = option_limit_price(self.broker, item["data"], item["quote"], "SELL_TO_OPEN")
            order = self._submit(
                item["data"],
                float(self.p.contracts),
                "SELL_TO_OPEN",
                price,
                allow_sell_to_open=True,
                risk_leg=risk_leg,
                underlying_positions={getattr(underlying, "_name", key): shares},
            )
            signal = {
                "symbol": item["meta"]["symbol"],
                "action": "SELL_TO_OPEN",
                "contracts": float(self.p.contracts),
                "strike": item["meta"]["strike"],
                "delta": item["quote"]["delta"],
                "submitted": order is not None,
                "skip_reason": getattr(self.broker, "_last_order_target_skip_reason", None),
            }
            self.last_signals.append(signal)
            print("[SampleCoveredCallStrategy] %s" % signal)
            if order is not None:
                occupied.add(key)
