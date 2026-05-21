# Live Adapter Contract

本文件覆盖 `live_trader/adapters/*_broker.py` 的当前契约。

## 1. Module Discovery Contract
1. 每个 live adapter 模块必须在同一文件中暴露:
- 一个 `BaseLiveBroker` 子类
- 一个 `BaseDataProvider` 子类
2. `LiveTrader` 通过反射在模块内查找这两个类。
3. 仅生成 Broker、不提供 DataProvider bridge 的 adapter 不符合当前装载契约。

## 2. Broker Minimum Contract
1. 必须遵守 `live_trader/adapters/base_broker.py`
2. 关键方法:
- `getvalue()`
- `_fetch_real_cash()`
- `get_position(data)`
- `get_current_price(data)`
- `get_pending_orders()`
- `_submit_order(data, volume, side, price)`
- `convert_order_proxy(raw_order)`
- `is_live_mode(context)`
3. 建议按市场覆盖 `get_sellable_position(data)`。

## 3. Pending Orders Contract
1. `get_pending_orders()` 返回项必须包含:
- `id`
- `symbol`
- `direction`
- `size`
2. `id` 必须可用于后续撤单。
3. `cancel_pending_order(order_id)` 失败时返回 `False`，不要把撤单失败变成致命异常。
4. 若原生 `orderId` 不稳定或缺失，必须提供可区分、可回查的兜底标识。

## 4. Stateless Constraints
1. 不维护长期本地 fake cash / fake position 作为事实来源。
2. 不在 adapter 内部自建跨回调拒单重试队列。
3. 状态查询优先实时向柜台或 SDK 拉取。

## 5. OrderProxy Runtime Contract
1. 必须实现 `BaseOrderProxy` 全部抽象方法，包括 `is_accepted()`
2. 当前运行时还要求代理对象暴露:
- `id`
- `status`
- `data`
- `executed`
3. `executed` 至少应提供:
- `size`
- `price`
- `value`
- `comm`
4. 最好同时提供 `executed.dt`，便于日志与成交通知使用。
5. `is_pending()` / `is_accepted()` 只能对真实在途态返回 `True`。过期、挂起/无效、撤单、拒单等不会继续成交的状态必须离开 pending，避免 `_pending_sells` 或 `_active_buys` 永久残留。

## 6. Data Matching Contract
1. `convert_order_proxy()` 在匹配 `data` 时，禁止使用 `in` 做模糊匹配。
2. 必须用明确、可解释的精确匹配逻辑。

## 7. Broker-Specific Launch Semantics
1. `launch(cls, conn_cfg, strategy_path, params, **kwargs)` 为 broker-specific 启动协议。
2. 不要假设所有 broker 对 `start_date`、schedule、回放模式的解释一致。
3. 若某 adapter 支持 live + replay/backtest 复合模式，应在该 adapter 文档或实现中明确说明。
4. 若 adapter 使用实盘 schedule 回调，应在运行 context 上设置 `schedule_rule` 或 `use_schedule`，避免基础 broker 将正常的 30m/1h 调度间隔误判为日内长中断。
