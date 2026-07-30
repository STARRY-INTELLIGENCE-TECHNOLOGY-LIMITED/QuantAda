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
3. `_submit_order()` 返回的代理若已是 Rejected/Canceled/Expired 等不会继续成交的终态，基础层会按未接受处理；BUY Rejected 会立即走统一降级重试。适配器必须准确映射这些状态，不能把同步废单伪装成 accepted/pending。
4. `_submit_order()` 返回 accepted/pending/completed 代理时必须提供非空 `id`；缺失 `id` 的代理会被基础层按未提交处理，避免留下不可跟踪的在途单或虚拟占资。
5. 建议按市场覆盖 `get_sellable_position(data)`。
6. 数量字段 `volume`、持仓 `.size`、pending `size`、`executed.size` 允许整数或小数。`LOT_SIZE` 和 `BROKER_LOT_LIMITS` 均为十进制正数语义；adapter 不得无条件转为 `int`，只可在目标券商/合约明确要求整数时做最终转换。

## 3. Pending Orders Contract
1. `get_pending_orders()` 返回项必须包含:
- `id`
- `symbol`
- `direction`
- `size`（可为正整数或正小数，必须保留券商返回精度）
2. `id` 必须可用于后续撤单。
3. `cancel_pending_order(order_id)` 失败时返回 `False`，不要把撤单失败变成致命异常。
4. 若原生 `orderId` 不稳定或缺失，必须提供可区分、可回查的兜底标识。
5. 若实时在途查询失败、断连或快照不完整，adapter 可安全返回 `[]`，但必须设置 `_last_pending_orders_fetch_failed=True` 与 `_last_pending_orders_fetch_error`；成功查询必须清零，避免 engine/executor 将“查不到”误判为“无在途”。
6. 上述失败标记仅用于 live runtime 判断快照可信度；它不是订单意图、不是重试队列，也不得被用于回测路径。回测 broker 应保持同步成交语义，不依赖 live pending-order 状态。
7. 实盘引擎每轮策略执行前以及基础层 `order_target_percent()` / `order_target_value()` 发单边界都会有界重试检查该快照；重试后仍异常或失败标记为真时必须失败关闭并跳过下单。可信在途数量必须计入目标仓位差额，允许继续执行可确认的剩余计划。回测路径不得查询该快照。

## 4. Stateless Constraints
1. 不维护长期本地 fake cash / fake position 作为事实来源。
2. 不在 adapter 内部自建跨回调拒单重试队列。
3. 状态查询优先实时向柜台或 SDK 拉取。
4. 卖单完成后的现金快照等待由 `common.order_executor` 统一处理；adapter 不应自行实现固定 sleep、轮询补买或卖后现金等待状态机。
5. 为支持通用卖后现金等待，adapter 只需保证:
- `get_rebalance_cash()` 或 `get_cash()` 返回当前真实可用于调仓的现金口径
- `get_current_price(data)` 能返回当前估算价格
- 卖单返回的 OrderProxy 可被执行器推断单笔委托数量，优先暴露 `submitted_size` / `requested_size`，或让原始对象保留在 `platform_order.volume` / `raw_order.volume` / `trade.order.totalQuantity`
- `submitted_size` / `requested_size` 只能表示该代理对应的单笔委托数量；基础层拆单批次总量使用 `batch_submitted_size`

## 5. Shared Execution Deadline Contract
1. Adapter 不得创建独立于当前 `run()` 的等待或重试预算；基础层会将 pending 查询、撤单、拆单和拒单降级限制在同一个 monotonic deadline 内。
2. 到达 deadline 后，adapter/base broker 必须停止发起新子单或重试，但不得撤销、遗忘或伪造已经受理的部分结果；剩余交易意图不持久化。
3. 异步拒单重试必须沿用原订单提交时捕获的 deadline，不能使用后续 schedule run 的新 deadline。
4. `_submit_order()`、`get_pending_orders()`、`cancel_pending_order()`、资产/持仓同步及 DataProvider 网络调用必须使用有限的 SDK/网络超时。框架 deadline 无法抢占已经阻塞的原生调用，因此单次超时必须明显短于默认 600 秒和实际 schedule 间隔预算。
5. 回测/优化 adapter 不得安装或依赖 live deadline，保持同步快速路径。

## 6. OrderProxy Runtime Contract
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
6. 框架层的成交、撤单、拒单、在途判断必须通过 `BaseOrderProxy.is_*()` 契约完成；`status` 只作为日志/告警文本，不得在 engine/base broker 中解释具体券商枚举。
7. `BaseLiveBroker` 拆分 SELL 时会在返回的首笔代理上附加 `batch_order_ids`、`batch_submitted_size` 和 `batch_submit_failed`，分别表示已受理子单 ID、已受理总数量和批次是否中途提交失败。adapter 不得重复拆单或自行写入这组基础层批次元数据。

## 7. Data Matching Contract
1. `convert_order_proxy()` 在匹配 `data` 时，禁止使用 `in` 做模糊匹配。
2. 必须用明确、可解释的精确匹配逻辑。

## 8. Broker-Specific Launch Semantics
1. `launch(cls, conn_cfg, strategy_path, params, **kwargs)` 为 broker-specific 启动协议。
2. 不要假设所有 broker 对 `start_date`、schedule、回放模式的解释一致。
3. 若某 adapter 支持 live + replay/backtest 复合模式，应在该 adapter 文档或实现中明确说明。
4. 若 adapter 使用实盘 schedule 回调，应在运行 context 上设置 `schedule_rule` 或 `use_schedule`，避免基础 broker 将正常的 30m/1h 调度间隔误判为日内长中断。
5. schedule 只兼容 `1d|Nm|Nh:HH:MM[:SS]`；配置 `Ns` 必须明确报错并引导使用长连接事件回调与 `timeframe='Seconds'`，不得静默降级为不运行策略。分钟级 SDK 轮询和单次调用超时应随实际周期缩短，避免跨入下一周期。
6. 对 24x7 市场使用 `KEEP_OVERNIGHT_ORDERS=True` 时，跨自然日及正常长周期 bar 间隔必须同时保留柜台订单及本地 `_active_buys`、`_pending_sells`、虚拟占资跟踪；仍以实时柜台快照和终态回调为事实来源。
