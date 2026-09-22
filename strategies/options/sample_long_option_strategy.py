"""多头期权样例：买开 Put 或 Call，并按到期/止盈卖平。

复制执行。密钥只放环境变量 FUTU_UNLOCK_PASSWORD，不要写进本文件。
`--params` 与类默认参数一致；平仓调试把 defense_dte 调到大于当前 DTE。

Long Put 开仓:
python run.py strategies.options.sample_long_option_strategy.SampleLongPutStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': -0.25, 'max_delta': -0.05, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'profit_take_multiple': 2.0, 'defense_dte': 7, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"

Long Call 开仓:
python run.py strategies.options.sample_long_option_strategy.SampleLongCallStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': 0.05, 'max_delta': 0.25, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'profit_take_multiple': 2.0, 'defense_dte': 7, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"

Long Put 平仓:
python run.py strategies.options.sample_long_option_strategy.SampleLongPutStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': -0.25, 'max_delta': -0.05, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'profit_take_multiple': 2.0, 'defense_dte': 60, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"

Long Call 平仓:
python run.py strategies.options.sample_long_option_strategy.SampleLongCallStrategy \\
  --data_source futu --symbols US.MARA --connect futu_broker:real --no_plot \\
  --params "{'min_dte': 30, 'max_dte': 45, 'min_delta': 0.05, 'max_delta': 0.25, \\
'min_iv_percentile': 0.0, 'max_spread_pct': 0.25, 'min_open_interest': 0.0, \\
'contracts': 1, 'profit_take_multiple': 2.0, 'defense_dte': 60, 'rebalance_when': 'daily'}" \\
  --config "{'FUTU_TRADE_ENV': 'REAL', 'FUTU_FILTER_TRDMARKET': 'US', \\
'FUTU_ACCOUNT_CURRENCY': 'USD', 'FUTU_TRADE_PASSWORD_ENV': 'FUTU_UNLOCK_PASSWORD'}"
"""

from __future__ import annotations

from common.options.analytics import safe_number
from common.data_view import (
    bar_datetime,
    pending_symbols,
    position_price,
    position_size,
    require_close_column,
)
from strategies.base_strategy import BaseStrategy
from strategies.options.support import (
    chain_window_reject_reason,
    dte_days,
    iter_option_rows,
    matches_chain_window,
    option_limit_price,
    reserved_underlying_keys,
)


class _SampleLongOptionStrategy(BaseStrategy):
    """按 DTE/Delta 买开单腿期权；只覆盖多头，不卖开。"""

    option_type = "PUT"
    params = {
        "min_dte": 30,
        "max_dte": 45,
        "min_delta": -0.25,
        "max_delta": -0.05,
        "min_iv_percentile": 0.0,
        "max_spread_pct": 0.25,
        "min_open_interest": 0.0,
        "contracts": 1,
        "profit_take_multiple": 2.0,
        "defense_dte": 7,
        "rebalance_when": "daily",
    }

    def __init__(self, broker, params=None):
        super().__init__(broker, params)
        self.last_signals = []

    def init(self):
        require_close_column(self.broker)

    def _submit(self, data, volume, effect, price):
        submitter = getattr(self.broker, "submit_option_order", None)
        if not callable(submitter):
            return None
        return submitter(data, volume, effect, price=price if price and price > 0 else None)

    def next(self):
        self.last_signals = []
        datas = list(getattr(self.broker, "datas", []) or [])
        if not datas:
            return
        current_dt = bar_datetime(datas[0], self.broker)
        if current_dt is None:
            return
        pending = pending_symbols(self.broker)
        wanted = str(self.option_type).upper()
        candidates = []
        rejects = {}
        occupied = reserved_underlying_keys(self.broker, {wanted})
        option_rows = 0

        for data, _row, meta, quote in iter_option_rows(self.broker, current_dt, {wanted}):
            option_rows += 1
            symbol = meta["symbol"]
            if symbol.upper() in pending:
                occupied.add(meta["underlying_key"])
                rejects["pending"] = rejects.get("pending", 0) + 1
                continue
            size = position_size(self.broker, data)
            if size > 0:
                occupied.add(meta["underlying_key"])
                dte = dte_days(meta["expiry"], current_dt)
                entry = position_price(self.broker, data)
                take_profit = entry * float(self.p.profit_take_multiple) if entry > 0 else 0.0
                should_close = (dte is not None and dte <= int(self.p.defense_dte)) or (
                    take_profit > 0 and quote["bid"] >= take_profit
                )
                signal = {
                    "symbol": symbol,
                    "action": "SELL_TO_CLOSE" if should_close else "HOLD_LONG",
                    "contracts": size,
                    "dte": dte,
                }
                if should_close:
                    order = self._submit(
                        data, size, "SELL_TO_CLOSE",
                        option_limit_price(self.broker, data, quote, "SELL_TO_CLOSE"),
                    )
                    signal["submitted"] = order is not None
                self.last_signals.append(signal)
                continue
            if size != 0:
                occupied.add(meta["underlying_key"])
                rejects["non_flat"] = rejects.get("non_flat", 0) + 1
                continue
            reason = chain_window_reject_reason(meta, quote, current_dt, self.p)
            if reason:
                rejects[reason] = rejects.get(reason, 0) + 1
                continue
            target_delta = (float(self.p.min_delta) + float(self.p.max_delta)) / 2.0
            candidates.append({
                "data": data,
                "meta": meta,
                "quote": quote,
                "score": (abs(quote["delta"] - target_delta), quote["spread_pct"], -quote["open_interest"], symbol),
            })

        openable = [
            item for item in candidates
            if item["meta"]["underlying_key"] not in occupied
        ]
        if not openable:
            print(
                "[%s] no candidates rows=%s rejects=%s occupied=%s"
                % (self.__class__.__name__, option_rows, rejects, sorted(occupied))
            )
            return
        if not self.should_execute_rebalance(
            target_symbols=[item["data"] for item in openable],
            rebalance_when=self.p.rebalance_when,
        ):
            print("[%s] rebalance skipped rows=%s" % (self.__class__.__name__, option_rows))
            return
        best = sorted(openable, key=lambda item: item["score"])[0]
        price = option_limit_price(self.broker, best["data"], best["quote"], "BUY_TO_OPEN")
        order = self._submit(best["data"], float(self.p.contracts), "BUY_TO_OPEN", price)
        skip_reason = getattr(self.broker, "_last_order_target_skip_reason", None)
        signal = {
            "symbol": best["meta"]["symbol"],
            "action": "BUY_TO_OPEN",
            "contracts": float(self.p.contracts),
            "strike": best["meta"]["strike"],
            "delta": best["quote"]["delta"],
            "ask": best["quote"]["ask"],
            "submitted": order is not None,
            "skip_reason": skip_reason,
        }
        self.last_signals.append(signal)
        print("[%s] %s" % (self.__class__.__name__, signal))


class SampleLongPutStrategy(_SampleLongOptionStrategy):
    """买开看跌期权样例。"""

    option_universe = ("PUT",)
    option_type = "PUT"


class SampleLongCallStrategy(_SampleLongOptionStrategy):
    """买开看涨期权样例。"""

    option_universe = ("CALL",)
    option_type = "CALL"
    params = {
        **_SampleLongOptionStrategy.params,
        "min_delta": 0.05,
        "max_delta": 0.25,
    }
