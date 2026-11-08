"""回测柜台的期权到期结算（显式物理交割或默认现金结算）；不进入实盘，也不保存策略意图。"""

from copy import copy
import math

import backtrader as bt
import pandas as pd

from common.options.analytics import parse_option_symbol, underlying_key
from common.options.lifecycle import OptionContract, settle_option_expiry


def _book_transfer(broker, owner, data, size, price, **info):
    """用引擎原生成交记账路径记录结算转移，不模拟市场滑点或交易佣金。"""
    submit = broker.buy if size > 0 else broker.sell
    order = submit(owner, data, size=abs(size), price=price, _checksubmit=False,
                   exectype=bt.Order.Historical, histnotify=True, **info)
    broker.pending.remove(order)
    original = broker.getcommissioninfo(data)
    transfer_comm = copy(original)
    transfer_comm.p = copy(original.p)
    transfer_comm.p.commission = 0.0
    previous = broker.comminfo.get(data._name)
    broker.comminfo[data._name] = transfer_comm
    try:
        broker._execute(order, ago=0, price=price)
    finally:
        if previous is None:
            broker.comminfo.pop(data._name, None)
        else:
            broker.comminfo[data._name] = previous
    if order.status != order.Completed:
        raise ValueError(f"Option settlement transfer failed: {data._name}")
    broker._bracketize(order)


def _datas_signature(datas):
    """用容器身份、长度和首尾 feed 判断行情列表是否变化。"""
    try:
        size = len(datas)
        first = datas[0] if size else None
        last = datas[-1] if size else None
    except TypeError:
        datas = tuple(datas)
        size = len(datas)
        first = datas[0] if size else None
        last = datas[-1] if size else None
    return (
        id(datas),
        size,
        id(first) if first is not None else 0,
        id(last) if last is not None else 0,
    )


def settlement_stock_feeds(owner):
    """缓存非期权 feed。列表不变时不再每个 bar 重新解析全部代码。"""
    datas = getattr(owner, "datas", None) or []
    signature = _datas_signature(datas)
    cached = getattr(owner, "_quantada_settlement_stocks", None)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == signature:
        return cached[1]
    stocks = {}
    for data in datas:
        name = getattr(data, "_name", "")
        if parse_option_symbol(name).get("option_type"):
            continue
        stocks[underlying_key(name)] = data
    try:
        owner._quantada_settlement_stocks = (signature, stocks)
    except Exception:
        pass
    return stocks


def _frame_clock(frame):
    """缓存正股日期索引，避免同一到期日反复 normalize 全历史。"""
    attrs = getattr(frame, "attrs", None)
    index_identity = id(frame.index)
    cached = attrs.get("_quantada_settlement_clock") if isinstance(attrs, dict) else None
    if isinstance(cached, tuple) and cached[0] == index_identity:
        return cached[1], cached[2]
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    normalized = index.normalize()
    if isinstance(attrs, dict):
        attrs["_quantada_settlement_clock"] = (index_identity, index, normalized)
    return index, normalized


def settle_expired_options(broker):
    """结算已到期期权持仓；模式取 broker.option_expiry_settlement（physical/cash）。

    physical：日线收盘结算，缺少到期日正股收盘价或担保不足时明确失败。
    同一根 K 到期的全部腿合并校验担保（如 ITM Put Spread 的指派与行权相互抵消），再统一记账。
    期权腿按内在价值平仓、正股按结算价交割，现金与按执行价交割完全一致，但指派盈亏归属期权交易。
    cash：未声明物理交割时的默认口径，期权腿按内在价值现金平仓、不交割正股，与到期估值标记一致。
    日线在到期 K 结算，日内周期在到期日之后首根 K 结算以避免偷看收盘；缺标的或价格时保持旧估值，不中断回测。
    """
    physical = getattr(broker, "option_expiry_settlement", None) == "physical"
    owner = broker.cerebro.runningstrats[0]
    clock_data = broker.cerebro.datas[0]
    current = pd.Timestamp(clock_data.datetime.datetime(0)).normalize()
    settle_on_expiry_bar = physical or clock_data._timeframe >= bt.TimeFrame.Days
    stocks = settlement_stock_feeds(owner)
    cash = float(broker.getcash())
    shares = {}
    shares_before = {}
    stock_deltas = {}
    legs = []
    for data, position in list(broker.positions.items()):
        if not position.size:
            continue
        parsed = parse_option_symbol(data._name)
        if not parsed.get("option_type"):
            continue
        expiry = pd.Timestamp(parsed["expiry"]).normalize()
        if pd.isna(expiry) or current < expiry or (current == expiry and not settle_on_expiry_bar):
            continue
        stock = stocks.get(underlying_key(parsed.get("underlying")))
        if stock is None:
            if not physical:
                continue
            raise ValueError(f"Physical settlement requires underlying feed: {data._name}")
        frame = stock.p.dataname
        index, normalized = _frame_clock(frame)
        visible = index <= current + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        # 现金结算与估值标记同口径：取到期日及之前最后一个有效收盘；物理交割必须是到期日当天收盘。
        day_mask = (normalized == expiry) if physical else (normalized <= expiry)
        closes = pd.to_numeric(frame.loc[day_mask & visible, "close"], errors="coerce")
        if not physical:
            closes = closes[closes > 0]
        if closes.empty:
            if not physical:
                continue
            raise ValueError(f"Physical settlement lacks expiry-day underlying close: {data._name}")
        spot = float(closes.iloc[-1])
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError(f"Physical settlement has invalid underlying close: {data._name}")
        multiplier = float(owner.get_contract_multiplier(data))
        contract = OptionContract(data._name, parsed["option_type"], parsed["strike"], expiry, multiplier,
                                  settlement="physical" if physical else "cash")
        if stock not in shares:
            shares[stock] = float(broker.getposition(stock).size)
            shares_before[stock] = shares[stock]
        result = settle_option_expiry(contract, position.size, spot, cash=cash,
                                      underlying_position=shares[stock], at=current)
        cash = result.cash_after
        shares[stock] = result.underlying_after
        if result.underlying_delta:
            key = (stock, spot)
            stock_deltas[key] = stock_deltas.get(key, 0.0) + result.underlying_delta
        size = float(position.size)
        legs.append((data, size, result, {
            "symbol": data._name, "event": result.event, "expiry": str(expiry.date()),
            "underlying": stock._name, "underlying_delta": result.underlying_delta,
            "cash_delta": result.cash_delta, "contracts": abs(size), "settlement_spot": spot,
            "settlement": contract.settlement,
        }))
    if not legs:
        return
    # 担保按本根 K 全部到期腿的净额校验，避免价差保护腿被逐腿结算误判为担保不足；现金结算沿用估值口径不校验。
    if physical and cash < -1e-8:
        raise ValueError(f"Physical settlement is not fully collateralized: {legs[-1][0]._name}")
    # 股票担保只约束物理交割新增的空头；现金结算不动正股，原有空头（如空股+保护性 Call）不是交割造成的。
    for stock, remaining in shares.items():
        if physical and remaining < min(shares_before[stock], 0.0) - 1e-8:
            raise ValueError(f"Physical settlement is not fully collateralized: {stock._name}")
    # 先平期权（只释放或扣减现金，不会触发开仓保证金），再按净股数交割；先卖后买避免中间现金为负。
    for data, size, result, _event in legs:
        _book_transfer(broker, owner, data, -size, result.intrinsic_value,
                       option_order_effect="BUY_TO_CLOSE" if size < 0 else "SELL_TO_CLOSE",
                       option_settlement_event=result.event)
    for (stock, spot), delta in sorted(stock_deltas.items(), key=lambda item: item[1]):
        if abs(delta) > 1e-12:
            _book_transfer(broker, owner, stock, delta, spot, option_settlement_event="OPTION_EXPIRY")
    broker.option_settlement_events.extend(leg[-1] for leg in legs)
    broker._get_value()
