# 实盘适配器契约

本文件覆盖 `live_trader/adapters/*_broker.py` 的当前契约。

配置边界：框架核心默认值保留在 `config.py`，Broker/Provider/Alarm 等责任域默认值存放在 `configs` 包的子模块中，由 `config.py` 显式导入并平铺导出；`configs/manager.py` 只合并 Broker 环境并提供报警状态函数。现有 `import config` 和 `run.py --config` 不变。adapter 的局部默认值仍应由所属模块安全地提供；一次性参数和兼容别名不得仅为了 CLI 使用而导出。

## 1. 模块发现契约
1. 每个 live adapter 模块只需暴露一个 `BaseLiveBroker` 子类。
2. `LiveTrader` 只通过反射查找 Broker，adapter 不承担 Provider 装载职责，因此 Broker 不再强依赖某个 Provider。
3. 历史行情和其他市场数据由 `data_providers` 包中的 `DataManager` 按 `data_source` 或平台默认值选择，并通过引擎现有的数据桥接接口提供给策略。
4. Broker 可以提供实时行情兜底或调用可选的预热数据，但不得定义或复制 `BaseDataProvider` 桥。Provider 的实现和凭据处理必须保留在 `data_providers`。

## 2. 券商最小契约
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
7. 基础层拆单是第一道边界；适配器的最终 `_submit_order()` 入口也必须按本轮有效配置再次拒绝超出 `BROKER_LOT_LIMITS` 的请求，不能让超限数量抵达券商 API。有效配置应优先读取实盘运行的配置快照，避免长进程重连时模块级默认值覆盖命令行覆盖值。
8. GM 适配器只服务中国市场，实盘调仓的 BUY/SELL 均使用 `OrderType_Market`。BUY 的 `price` 应至少覆盖实时最优卖价，SELL 使用实时行情作为保护价；该字段是市价保护价，不会把订单变回限价单。资金预检查、虚拟占资和拒单退款均按本次保护价估算。回测仍使用同步市价语义。
9. IBKR `CRYPTO`（PAXOS）合约的历史数据使用 `AGGTRADES`；市价委托必须使用合约要求的现金数量精度（USD 分）并设置明确的有效期（当前为 `IOC`），不得同时发送非零 `totalQuantity`。订单代理应通过 `submitted_size` 保留基础层所需的币数量，不能因柜台现金数量委托的 wire 数量为零而误判未受理。

## 3. 在途委托契约
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
7. 实盘引擎每轮策略执行前、策略资金/仓位盘点时，以及基础层 `order_target_percent()` / `order_target_value()` 发单边界都会检查该快照；异常或失败标记为真时必须失败关闭并跳过当轮调仓，不得将其解释为空在途。可信在途数量必须计入目标仓位差额，允许继续执行可确认的剩余计划。回测路径不得查询该快照。
8. 若券商原始在途记录缺失/空 `id`、缺失 `symbol`、方向未知、剩余数量为负或非数值，或剩余数量为零但缺少可验证证据，adapter 必须将整份 pending 快照标记为不可信；不得静默跳过坏记录或把未知方向默认解释为 SELL。已知非终态但短暂报告 `remaining=0` 时，只有同时提供正的 `totalQuantity` 与 `filled` 才可判断：`filled >= totalQuantity` 安全排除，`filled < totalQuantity` 按 `totalQuantity-filled` 保守计入在途；缺少该证据仍必须失败关闭。

## 4. 无状态约束
1. 不维护长期本地 fake cash / fake position 作为事实来源。
2. 不在 adapter 内部自建跨回调拒单重试队列。
3. 状态查询优先实时向柜台或 SDK 拉取。若 adapter 的 schedule/on_bar 与订单回调由同一个 SDK 事件线程串行处理，当前 run 内的 SELL 等待不得以事件缓存持仓作为实时对账依据；应使用同步柜台持仓查询，查询失败时抛出或标记快照不可信，禁止返回静默空仓。基类持仓/可卖仓位查询也不得把 adapter 异常吞成真实的零仓。实盘信号日志不得使用固定的调度 `context.now` 作为默认时间。
4. 若 SDK/TCP 已连接但账户摘要或持仓订阅尚未同步，adapter 必须将其暴露为当前会话的短生命周期健康失败，不能把空结果静默解释为真实的零现金/空仓；实盘运行应有界重试后跳过当轮。该健康标记不得进入回测/优化路径，也不得保存交易意图。
5. 多账户 adapter 必须让现金、持仓、pending 与下单使用同一明确账户范围。GM adapter 当前只支持券商会话绑定的单一账户，使用 SDK 默认单账户语义，不增加账户选择配置；IB 等多账户 adapter 仍须按其连接配置明确筛选目标账户。已明确筛选目标账户后，其他账户有仓而目标账户为空是合法的零仓，不得误报为快照故障。
6. 卖单完成后的现金快照等待由 `common.order_executor` 统一处理；adapter 不应自行实现固定 sleep、轮询补买或卖后现金等待状态机。
7. 为支持通用卖后现金等待，adapter 只需保证:
- `get_rebalance_cash()` 或 `get_cash()` 返回当前真实可用于调仓的现金口径
- `get_current_price(data)` 能返回当前估算价格
- 卖单返回的 OrderProxy 可被执行器推断单笔委托数量，优先暴露 `submitted_size` / `requested_size`，或让原始对象保留在 `platform_order.volume` / `raw_order.volume` / `trade.order.totalQuantity`
- `submitted_size` / `requested_size` 只能表示该代理对应的单笔委托数量；基础层拆单批次总量使用 `batch_submitted_size`

## 5. 共享执行截止时间契约
1. Adapter 不得创建独立于当前 `run()` 的等待或重试预算；基础层会将 pending 查询、撤单、拆单和拒单降级限制在同一个 monotonic deadline 内。
2. 到达 deadline 后，adapter/base broker 必须停止发起新子单或重试，但不得撤销、遗忘或伪造已经受理的部分结果；剩余交易意图不持久化。
3. 异步拒单重试必须沿用原订单提交时捕获的 deadline，不能使用后续 schedule run 的新 deadline。
4. `_submit_order()`、`get_pending_orders()`、`cancel_pending_order()`、资产/持仓同步及 DataProvider 网络调用必须使用有限的 SDK/网络超时。框架 deadline 无法抢占已经阻塞的原生调用，因此单次超时必须明显短于默认 600 秒和实际 schedule 间隔预算。
5. 回测/优化 adapter 不得安装或依赖 live deadline，保持同步快速路径。

## 6. OrderProxy 运行时契约
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

## 7. 数据匹配契约
1. `convert_order_proxy()` 在匹配 `data` 时，禁止使用 `in` 做模糊匹配。
2. 必须用明确、可解释的精确匹配逻辑。

## 8. 券商专属启动语义
1. `launch(cls, conn_cfg, strategy_path, params, **kwargs)` 为 broker-specific 启动协议。
2. 不要假设所有 broker 对 `start_date`、schedule、回放模式的解释一致。
3. 若某 adapter 支持 live + replay/backtest 复合模式，应在该 adapter 文档或实现中明确说明。
4. 若 adapter 使用实盘 schedule 回调，应在运行 context 上设置 `schedule_rule` 或 `use_schedule`，避免基础 broker 将正常的 30m/1h 调度间隔误判为日内长中断。
5. schedule 只兼容 `1d|Nm|Nh:HH:MM[:SS]`；配置 `Ns` 必须明确报错并引导使用长连接事件回调与 `timeframe='Seconds'`，不得静默降级为不运行策略。分钟级 SDK 轮询和单次调用超时应随实际周期缩短，避免跨入下一周期。
6. 若 schedule 回调与订单状态回调共享单线程 SDK 事件循环（包括 GM schedule、IB `ib.sleep()` 主循环），不能在回调线程同步执行包含 SELL 等待、现金同步和最终 BUY 的完整实盘运行；必须将 slot 调度到单个短生命周期工作线程，并让 SDK 回调线程立即返回。重叠 slot 只记录并跳过；回测/优化不得创建该工作线程。
7. 对 24x7 市场使用 `KEEP_OVERNIGHT_ORDERS=True` 时，跨自然日及正常长周期 bar 间隔必须同时保留柜台订单及本地 `_active_buys`、`_pending_sells`、虚拟占资跟踪；仍以实时柜台快照和终态回调为事实来源。
