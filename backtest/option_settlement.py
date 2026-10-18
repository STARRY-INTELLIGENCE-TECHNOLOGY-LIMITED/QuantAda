"""回测柜台的日线期权物理交割；不进入实盘，也不保存策略意图。"""

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


def settle_physical_options(broker):
    """日线收盘结算已到期持仓；缺少到期日正股收盘价时明确失败。"""
    owner = broker.cerebro.runningstrats[0]
    current = pd.Timestamp(broker.cerebro.datas[0].datetime.datetime(0)).normalize()
    stocks = settlement_stock_feeds(owner)
    events = []
    for data, position in list(broker.positions.items()):
        if not position.size:
            continue
        parsed = parse_option_symbol(data._name)
        if not parsed.get("option_type"):
            continue
        expiry = pd.Timestamp(parsed["expiry"]).normalize()
        if current < expiry:
            continue
        stock = stocks.get(underlying_key(parsed.get("underlying")))
        if stock is None:
            raise ValueError(f"Physical settlement requires underlying feed: {data._name}")
        frame = stock.p.dataname
        index, normalized = _frame_clock(frame)
        at_expiry = frame.loc[(normalized == expiry) & (index <= current + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1))]
        if at_expiry.empty:
            raise ValueError(f"Physical settlement lacks expiry-day underlying close: {data._name}")
        spot = float(at_expiry["close"].iloc[-1])
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError(f"Physical settlement has invalid underlying close: {data._name}")
        multiplier = float(owner.get_contract_multiplier(data))
        contract = OptionContract(data._name, parsed["option_type"], parsed["strike"], expiry, multiplier)
        shares = float(broker.getposition(stock).size)
        result = settle_option_expiry(contract, position.size, spot, cash=broker.getcash(),
                                      underlying_position=shares, at=current)
        if result.cash_after < -1e-8 or result.underlying_after < -1e-8:
            raise ValueError(f"Physical settlement is not fully collateralized: {data._name}")
        size = float(position.size)
        event = {"symbol": data._name, "event": result.event, "expiry": str(expiry.date()),
                 "underlying": stock._name, "underlying_delta": result.underlying_delta,
                 "cash_delta": result.cash_delta, "contracts": abs(size), "settlement_spot": spot}
        # 期权归零与股票按执行价交割构成一笔结算，不把两者当作市价买卖。
        _book_transfer(broker, owner, data, -size, 0.0,
                       option_order_effect="BUY_TO_CLOSE" if size < 0 else "SELL_TO_CLOSE",
                       option_settlement_event=result.event)
        if result.underlying_delta:
            _book_transfer(broker, owner, stock, result.underlying_delta, contract.strike,
                           option_settlement_event=result.event)
        events.append(event)
    if events:
        broker.option_settlement_events.extend(events)
        broker._get_value()
