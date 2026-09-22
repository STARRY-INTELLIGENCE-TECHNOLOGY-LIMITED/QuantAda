"""定义风险 Put Credit Spread 样例：只通过券商原子组合开仓/平仓。

复制执行。密钥只放环境变量 FUTU_UNLOCK_PASSWORD，不要写进本文件。
`--params` 与类默认参数一致；平仓调试把 defense_dte 调到大于当前 DTE。

开仓:
python run.py strategies.options.sample_put_credit_spread_strategy.SamplePutCreditSpreadStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': -0.25, 'max_delta': -0.05, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'protective_put_delta': -0.10, \\
'protective_put_min_strike_gap_pct': 0.05, 'profit_take_fraction': 0.50, \\
'defense_dte': 21, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"

平仓:
python run.py strategies.options.sample_put_credit_spread_strategy.SamplePutCreditSpreadStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': -0.25, 'max_delta': -0.05, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'protective_put_delta': -0.10, \\
'protective_put_min_strike_gap_pct': 0.05, 'profit_take_fraction': 0.50, \\
'defense_dte': 60, 'rebalance_when': 'daily'}" \\
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
    held_protective_put,
    iter_option_rows,
    matches_chain_window,
    option_limit_price,
    reserved_underlying_keys,
    underlying_feed,
)


class SamplePutCreditSpreadStrategy(BaseStrategy):
    """卖出较高执行价 Put，同时买入更低执行价保护腿。"""

    option_universe = ("PUT",)
    params = {
        "min_dte": 30,
        "max_dte": 45,
        "min_delta": -0.25,
        "max_delta": -0.05,
        "min_iv_percentile": 0.0,
        "max_spread_pct": 0.25,
        "min_open_interest": 0.0,
        "contracts": 1,
        "protective_put_delta": -0.10,
        "protective_put_min_strike_gap_pct": 0.05,
        "profit_take_fraction": 0.50,
        "defense_dte": 21,
        "rebalance_when": "daily",
    }

    def __init__(self, broker, params=None):
        super().__init__(broker, params)
        self.last_signals = []

    def init(self):
        require_close_column(self.broker)

    def _find_protective_put(self, short_item, current_dt, pending):
        target_delta = float(self.p.protective_put_delta)
        min_gap = float(self.p.protective_put_min_strike_gap_pct)
        candidates = []
        for data, _row, meta, quote in iter_option_rows(self.broker, current_dt, {"PUT"}):
            if meta["symbol"].upper() in pending:
                continue
            if meta["underlying_key"] != short_item["meta"]["underlying_key"]:
                continue
            if meta["expiry"] != short_item["meta"]["expiry"]:
                continue
            if meta["strike"] >= short_item["meta"]["strike"] * (1.0 - min_gap):
                continue
            if quote["ask"] <= 0 or quote["bid"] <= 0:
                continue
            candidates.append({
                "data": data,
                "meta": meta,
                "quote": quote,
                "delta_distance": abs(quote["delta"] - target_delta),
            })
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (item["delta_distance"], item["meta"]["strike"], item["meta"]["symbol"]))[0]

    def _held_protective_put(self, short_meta, current_dt):
        return held_protective_put(self.broker, short_meta, current_dt)

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
        occupied = reserved_underlying_keys(self.broker, {"PUT"})

        for data, _row, meta, quote in iter_option_rows(self.broker, current_dt, {"PUT"}):
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
                    protective = self._held_protective_put(meta, current_dt)
                    spread = getattr(self.broker, "submit_option_spread", None)
                    order = None
                    if protective is not None and protective.get("quote") is None:
                        signal["blocked_reason"] = "protective_put_quote_unavailable"
                    elif protective is not None and callable(spread):
                        close_buy = option_limit_price(self.broker, data, quote, "BUY_TO_CLOSE")
                        close_sell = option_limit_price(
                            self.broker, protective["data"], protective["quote"], "SELL_TO_CLOSE"
                        )
                        order = spread(
                            [
                                {
                                    "data": data,
                                    "volume": abs(size),
                                    "effect": "BUY_TO_CLOSE",
                                    "price": close_buy,
                                },
                                {
                                    "data": protective["data"],
                                    "volume": abs(size),
                                    "effect": "SELL_TO_CLOSE",
                                    "price": close_sell,
                                },
                            ],
                            volume=abs(size),
                        )
                        signal["spread"] = True
                        signal["protective_symbol"] = protective["meta"]["symbol"]
                    if order is None and signal.get("blocked_reason") != "protective_put_quote_unavailable":
                        submitter = getattr(self.broker, "submit_option_order", None)
                        if callable(submitter):
                            order = submitter(
                                data, abs(size), "BUY_TO_CLOSE",
                                price=option_limit_price(self.broker, data, quote, "BUY_TO_CLOSE"),
                            )
                            signal["protective_close_pending"] = order is not None
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
            "[SamplePutCreditSpreadStrategy] occupied=%s candidates=%s signals=%s"
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
                continue
            protective = self._find_protective_put(item, current_dt, pending)
            if protective is None:
                signal = {
                    "symbol": item["meta"]["symbol"],
                    "action": "SELL_TO_OPEN",
                    "submitted": False,
                    "blocked_reason": "protective_put_unavailable",
                }
                self.last_signals.append(signal)
                print("[SamplePutCreditSpreadStrategy] %s" % signal)
                continue
            spot = safe_number(self.broker.get_current_price(underlying), 0.0)
            multiplier = safe_number(self.broker.get_contract_multiplier(item["data"]), 0.0)
            protective_multiplier = safe_number(
                self.broker.get_contract_multiplier(protective["data"]), 0.0
            )
            if spot <= 0 or multiplier <= 0 or abs(multiplier - protective_multiplier) > 1e-12:
                continue
            short_leg = OptionRiskLeg(
                item["meta"]["symbol"],
                getattr(underlying, "_name", key),
                "PUT",
                -float(self.p.contracts),
                item["meta"]["strike"],
                item["quote"]["bid"],
                spot,
                multiplier,
                expiry=item["meta"]["expiry"],
                historical_volatility=item["quote"]["historical_volatility"],
                price_shock=0.20,
            )
            long_leg = OptionRiskLeg(
                protective["meta"]["symbol"],
                getattr(underlying, "_name", key),
                "PUT",
                float(self.p.contracts),
                protective["meta"]["strike"],
                protective["quote"]["ask"],
                spot,
                protective_multiplier,
                expiry=protective["meta"]["expiry"],
                historical_volatility=item["quote"]["historical_volatility"],
                price_shock=0.20,
            )
            spread = getattr(self.broker, "submit_option_spread", None)
            order = None
            short_price = option_limit_price(self.broker, item["data"], item["quote"], "SELL_TO_OPEN")
            long_price = option_limit_price(self.broker, protective["data"], protective["quote"], "BUY_TO_OPEN")
            if callable(spread):
                order = spread(
                    [
                        {
                            "data": item["data"],
                            "volume": float(self.p.contracts),
                            "effect": "SELL_TO_OPEN",
                            "price": short_price,
                            "risk_leg": short_leg,
                            "allow_sell_to_open": True,
                            "underlying_positions": {
                                getattr(underlying, "_name", key): position_size(self.broker, underlying)
                            },
                        },
                        {
                            "data": protective["data"],
                            "volume": float(self.p.contracts),
                            "effect": "BUY_TO_OPEN",
                            "price": long_price,
                            "risk_leg": long_leg,
                        },
                    ],
                    volume=float(self.p.contracts),
                )
            signal = {
                "symbol": item["meta"]["symbol"],
                "action": "SELL_TO_OPEN",
                "contracts": float(self.p.contracts),
                "strike": item["meta"]["strike"],
                "protective_symbol": protective["meta"]["symbol"],
                "protective_strike": protective["meta"]["strike"],
                "spread": True,
                "submitted": order is not None,
                "skip_reason": getattr(self.broker, "_last_order_target_skip_reason", None),
            }
            self.last_signals.append(signal)
            print("[SamplePutCreditSpreadStrategy] %s" % signal)
            if order is not None:
                occupied.add(key)
