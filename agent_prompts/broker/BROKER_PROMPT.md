# QuantAda 框架 - 券商适配器 AI 生成指令

## 🤖 系统角色定义
你现在是一位拥有 10 年经验的企业级量化交易系统架构师。你需要为一个名为 **QuantAda** 的开源全天候量化交易框架编写一个新的底层券商适配器（Broker Adapter）。
请仔细阅读以下【输入信息】与【接口契约】，并严格生成健壮、符合类型提示（Type Hints）的 Python 代码。

---

## 📥 输入信息
- **目标券商名称**: [请在此处填入券商名称，例如：Longbridge / Futubull / Charles Schwab]
- **目标券商 API 文档**: [请在此处粘贴或上传该券商的官方 Python SDK 文档、发单接口、资产查询接口、状态流转等]

---

## 🏛️ 核心架构约束

1. **继承基类**: 你的主类必须命名为 `[BrokerName]Broker`，并且严格继承自 `live_trader.adapters.base_broker.BaseLiveBroker`。
2. **模块装载契约**: `live_trader.engine.LiveTrader` 会在同一个 adapter 模块中同时反射 Broker 和 DataProvider。因此输出文件中除了 `[BrokerName]Broker` 外，还必须同时暴露一个 `BaseDataProvider` 子类（可为薄封装），并兼容 `get_history(...)` 调用。
3. **绝对无状态 (Stateless)**: QuantAda 已移除 `deferred/buffered` 买单队列。适配器内部**严禁**维护任何类似 `self.local_cash` 或 `self.local_positions` 的缓存变量，也**严禁**自行实现跨回调重试队列。所有状态查询必须实时通过 API 向物理柜台发起。
   若 SDK/TCP 已连接但账户摘要或持仓订阅尚未同步，必须暴露当前会话的短生命周期健康失败并让实盘当轮有界重试后失败关闭；禁止把空快照静默解释为真实的零现金/空仓。该健康状态不得进入回测/优化路径或保存交易意图。
4. **数据对象解包**: 框架传入的 `data` 参数是一个代理对象（DataFeedProxy）。获取标的代码时，必须使用 `data._name`，并在与券商 API 交互前，根据需要进行格式化（例如截取基础代码 `data._name.split('.')[0].upper()`）。
5. **卖出可用仓位约束**: 对存在 T+1 或可卖冻结语义的市场，必须提供准确可卖仓位（建议实现/覆盖 `get_sellable_position`），不要仅用总仓位代替可卖仓位。

---

## 🛑 必须实现的接口契约

你必须严格实现以下 `@abstractmethod`，严禁修改方法签名：

### 1. 资产与持仓查询
- `getvalue(self) -> float`: 获取当前账户总权益（Net Liquidation Value）。可调用父类的 `self._get_portfolio_nav()` 或直接调用券商 API 获取。
- `_fetch_real_cash(self) -> float`: 实时向柜台请求当前可用于开新仓的真实购买力（现金）。如券商口径不含在途冻结，需在适配器层补充扣减逻辑。
- `get_position(self, data)`: 获取指定标的持仓。必须返回一个拥有 `.size` (持仓数量，可为整数或小数) 和 `.price` (成本价) 属性的对象（可使用 `SimpleNamespace` 模拟）。若市场有可卖限制，建议同时暴露 `.sellable`。若 SDK 在同一事件线程串行执行 schedule 和订单/持仓回调，实盘实现必须在当前 run 的 SELL 等待期间使用同步柜台查询，不能读取被 schedule 阻塞的旧 context 缓存；查询失败必须暴露为不可信状态，不能当作零持仓。基类会让持仓/可卖仓位查询异常失败关闭，adapter 不应依赖异常被静默转换为零仓。
- `get_sellable_position(self, data)`（建议覆盖）: 返回当前真实可卖仓位；若不覆盖，基类会退化为 `size`。
- `get_current_price(self, data) -> float`: 获取指定标的实时盘口价或最新快照价。若获取失败、断流或停牌，必须安全返回 `0.0`，严禁抛出异常。
- 卖单完成后的现金快照等待由 `common.order_executor` 统一处理；适配器不要自行实现固定 sleep、轮询补买或卖后现金等待状态机。若需要支持更准确的通用等待，确保 `get_rebalance_cash()` / `get_cash()`、`get_current_price(data)` 和订单代理的委托数量字段可用。

### 2. 订单系统
- `get_pending_orders(self) -> list`: 获取所有未完成的在途订单。**必须返回以下严格格式的字典列表**：
  `[{'id': '123', 'symbol': 'AAPL', 'direction': 'BUY' 或 'SELL', 'size': 100}, ...]`，其中 `size` 可为整数或小数且必须保留券商精度。
  若实时查询失败、断连或快照不完整，可安全返回 `[]`，但必须设置 `self._last_pending_orders_fetch_failed = True` 和 `self._last_pending_orders_fetch_error = error`；成功查询必须清零，避免框架把“查不到”误判为“无在途”。
  原始记录若缺失/空 `id`、缺失 `symbol`、方向未知、剩余数量非正或非数值，也属于快照不完整：必须使整份快照失败关闭，不能静默跳过坏记录或将未知方向默认映射为 SELL。
  该失败标记只能表示当前快照可信度，不得保存交易意图、不得驱动跨 K 重试、不得用于回测路径；实盘引擎每轮策略执行前、策略资金盘点时和基础层目标下单边界都必须检查该标记，失败时关闭并跳过当轮调仓；可信在途数量必须计入目标差额以继续执行剩余计划；回测应保持订单同步成交语义且不得查询 live pending。
- `cancel_pending_order(self, order_id: str) -> bool`: 按订单ID发起撤单。返回是否成功发起撤单请求（True/False）。该接口用于引擎在交易日首轮前清理隔夜在途单。
- `_submit_order(self, data, volume, side: str, price: float)`: 核心发单路由。`volume` 可为整数或小数，只有目标券商/合约明确要求整数时才能在最终提交边界转换为 `int`；`side` 为 `'BUY'` 或 `'SELL'`。将其翻译为目标券商的结构体并发起发单请求，发单成功后返回自定义的 `BaseOrderProxy` 子类实例，失败返回 `None`。
  若券商同步返回 Rejected/Canceled/Expired 等不会继续成交的订单对象，代理必须准确暴露该终态；基础层会按未接受处理，BUY Rejected 会立即进入统一降级重试。不要把同步废单映射成 accepted/pending，否则会出现“实盘信号已打印但柜台没有委托”的误判。
  若返回 accepted/pending/completed 代理，`id` 必须非空且稳定；缺失 `id` 的代理会被基础层按未提交处理，避免留下不可跟踪的在途单或虚拟占资。

### 3. 状态转换器与代理类
- **必须创建一个子类**继承自 `live_trader.adapters.base_broker.BaseOrderProxy`，并实现其所有的 `@abstractmethod` 属性和方法（包括 `is_accepted()`）。
- **当前运行时还要求代理对象暴露以下属性/字段**:
  - `status`: 原始或标准化后的订单状态
  - `executed`: 一个带 `size`, `price`, `value`, `comm`，并最好带 `dt` 的对象
  - `data`: 匹配到的框架 data 对象（匹配失败时可为 `None`）
- 框架层只通过 `BaseOrderProxy.is_*()` 判断成交、拒单、撤单、在途和 accepted；`status` 仅用于日志/告警展示。适配器必须在 proxy 内完成券商状态枚举到统一语义的翻译，不要要求 engine/base broker 识别具体券商状态。
- `id` 必须稳定且可用于后续撤单；若券商原生 `orderId` 可能缺失，应提供可区分的兜底标识。
- 为便于通用执行器估算本轮卖出释放资金，订单代理应尽量暴露单笔真实委托数量字段，例如 `submitted_size` / `requested_size`，或保留原始对象的 `platform_order.volume` / `raw_order.volume` / `trade.order.totalQuantity`。不得用批次总量覆盖单笔字段；基础层拆单会在首笔代理附加 `batch_order_ids`、`batch_submitted_size`、`batch_submit_failed`，adapter 不得重复拆单或自行维护这些批次元数据。
- `convert_order_proxy(self, raw_order) -> BaseOrderProxy`: 引擎回调入口。将目标券商特有的 Trade/Order 回调对象，解析并转换为上述自定义的 `BaseOrderProxy` 对象。**注意：匹配归属的 data 对象时，严禁使用 `in` 进行模糊匹配，必须使用精确的字符串等于判定。**
- `is_pending()` / `is_accepted()` 只能对真实在途态返回 `True`。过期、挂起/无效、撤单、拒单等不会继续成交的状态必须离开 pending，避免 `_pending_sells` 或 `_active_buys` 永久残留。

### 4. 运行环境适配
- `@staticmethod` `is_live_mode(context) -> bool`: 判断当前上下文是否为实盘模式。
- `@classmethod` `launch(cls, conn_cfg: dict, strategy_path: str, params: dict, **kwargs)`: [可选实现] 命令行实盘启动入口，负责初始化券商 SDK、建立连接并挂载事件循环。
- 若 adapter 使用实盘 schedule 回调，应在运行 context 上设置 `schedule_rule` 或 `use_schedule`，避免基础 broker 将正常的 30m/1h 调度间隔误判为日内长中断。
- 多账户券商的现金、持仓、pending 和下单必须使用同一明确账户。GM adapter 当前只支持券商会话绑定的单一账户，使用 SDK 默认单账户语义，不增加账户选择配置；IB 等多账户 adapter 仍须按其连接配置明确筛选目标账户。明确筛选目标账户后，其他账户有仓而目标账户为空属于合法零仓，不能误报为快照故障。
- schedule 只兼容 `1d|Nm|Nh:HH:MM[:SS]`；配置 `Ns` 必须明确报错，并引导使用长连接事件回调与 `timeframe='Seconds'`。分钟级事件循环轮询和 SDK 超时必须随周期缩短，不能让一次调用跨过下一轮。
- `DataProvider` 子类: 必须让引擎能通过当前 adapter 模块直接发现；如果历史数据能力来自现有 provider，也请在本文件中提供桥接类，而不是只写说明文字。

---

## ⚙️ 与当前框架一致的执行语义（必须遵守）
1. 买单拒绝后的降级重提由 `BaseLiveBroker.on_order_status` 统一处理（默认最多 10 次：前 5 次 `LOT_SIZE` 阶梯降级 + 后 5 次几何降级）；适配器不要额外叠加自己的“拒单队列”。
2. 禁止实现或依赖以下旧机制: `process_deferred_orders`、`reconcile_buffered_retries`、`_deferred_orders`、`_buffered_rejected_retries`。
3. 若券商返回 `Inactive/Cancelled/Rejected/Expired/Suspended` 等语义有差异，必须在 `BaseOrderProxy` 中准确映射，否则会破坏统一降级流程或造成本地 pending 永久等待。
4. 引擎会在实盘每个自然日首次 `run`、拉数据前尝试清理隔夜在途单（由 `config.KEEP_OVERNIGHT_ORDERS` 控制）。适配器必须保证:
- `get_pending_orders` 中 `id` 可用于撤单
- `cancel_pending_order` 幂等、异常安全（失败返回 False，不抛出致命异常）
5. 当前拒单重试语义为“无状态 + 当场重提”: 前 5 次按 `LOT_SIZE` 线性降级，后 5 次按几何倍数降级；适配器侧必须提供真实现金口径，避免重试阶段出现系统性偏差。
6. `LOT_SIZE` 与 `BROKER_LOT_LIMITS` 都允许正整数或正小数（币市可用 `LOT_SIZE=0.00000001`）；`BROKER_LOT_LIMITS=0` 表示不限制，BUY/SELL 双向生效。基础 broker 会使用十进制对齐并在当前调用内拆单、独立跟踪；数量、持仓、pending 和成交字段不得无条件 `int()`。任一 BUY/SELL 子单（包括首笔）同步失败或拒绝时，基础层会在原 run deadline 内复用统一的 5 次线性 + 5 次几何降级；adapter 实际受理量小于请求量时按真实量记账并继续剩余差额，适配器不得再次拆单。SELL 清仓尾单允许保留碎股；部分 SELL 受理后执行器必须有界等待并按实际总受理量对账，达到该部分目标后让 BUY 按真实现金保守截断，未达到目标或 pending 不可信时才阻断 BUY。耗尽后 LOG + IM 报 ERROR，不保存剩余意图。框架不另设隐藏的拆单笔数上限，配置值必须来自券商真实限制；回测/优化保持单笔同步路径。
7. 柜台拒单详情必须同时进入本地日志和 IM；原因文本只用于审计与人工配置修正，不得用 NLP 文本匹配驱动自动下单。
8. 已知券商维护型连接失败仅记录日志并自愈，不直接推异常 IM；若 schedule prewarm 或实际 run 时刻仍不可用，需分别推送按 slot 去重的 ERROR 告警，但不得把该 slot 误记为已执行。schedule 告警按自然日执行，不按星期筛选，默认覆盖 7x24 时段。
9. 适配器和执行器必须区分 live/backtest：实盘以柜台现实、持仓/现金对账和短生命周期健康标记恢复；回测不得进入实时 pending 查询、卖单等待、现金结算等待或 broker 同步路径。
10. 卖后现金等待、滚动买入和最终补齐属于 `common.order_executor` 职责；adapter 不要重复实现这些流程，只暴露真实现金、价格、在途订单和订单代理字段。
11. 使用 SDK 事件循环的实盘 adapter 必须把 SDK 线程/轮询/协作等待函数抛出的非人工 `SystemExit` 当作 session 退出并交给 Phoenix 重启；不要让 nohup 长进程被 SDK 直接带退出。GM `gmi_poll()` 的普通非零返回值按官方循环语义限频记录并继续，不能因无消息的 `-1` 持续重建 worker；明确 shutdown、`SystemExit` 和连接健康超时仍须重启。人工 `KeyboardInterrupt` 仍应退出。schedule prewarm 和正式 run 都应按目标 slot 去重，长进程 warning/error/Phoenix 生命周期日志应通过 `common.live_runtime.runtime_print()` 带时间戳。GM 这类进程内 SDK 若 init 失败，应先重绑 token/server/callback 并 soft reset；连续 init 失败可 re-exec 当前进程作为最后自愈。
12. 通过 `run.py --connect` 运行时，通用父进程监督器负责进程级保活与探活；worker 进入 broker SDK 前必须推送一次 `STARTED`，同一 worker 进程内不得重复发送；adapter 应在 native SDK 初始化/连接阶段上报短生命周期健康状态和有界超时。监督器发现 worker 退出、heartbeat 停滞或健康期限超时后，以原始命令冷启动并记录退出码/信号；连接维护等降噪策略必须通过结构化故障类别传递，不得解析状态或原因文本。受监督 worker 的内部重启不重复推 `STOPPED` / `DEAD`；操作者 `SIGINT` 安全退出才由 worker 推送一次 `STOPPED`。配置 `1d` schedule 时，每个自然日在正式 slot 前 30 分钟固定推送一次仅表示 worker 存活的 `ALIVE`。GM 维护期连接故障可在该同一边界（或更早 prewarm）前低频探测，但不能停探；边界外的 `gmi_poll=-1`、1200/1201 行情连接及 1100 交易连接维护状态不得周期性刷 warning/error 日志，边界到达后恢复有限日志并通过 heartbeat/干净重建切回积极恢复。区间 schedule 的等待不得跨 slot，无有效 schedule 时不得降频。监督器不得保存或重放交易意图，回测/优化不得启动该链路。
13. 每次实盘 `run` 从隔夜清理前共享一个 monotonic deadline：默认最多 600 秒，分钟/小时 schedule 或 `Minutes|Seconds` timeframe 自动缩短为触发间隔的 80%。pending 查询、撤单、数据恢复、SELL 等待、资金等待、BUY/SELL 拆单和 BUY 降级都必须在该 deadline 内；到期后停止发起新动作但保留已受理/成交的部分结果。异步拒单只能沿用原订单提交时的 deadline，不能借下一轮预算重放旧意图。所有 SDK 查询/撤单/发单接口必须设置明显短于该预算的有限超时；回测/优化不得进入此机制。
14. 24x7 市场配置 `KEEP_OVERNIGHT_ORDERS=True` 时，跨自然日必须保留远端委托以及本地 `_active_buys`、`_pending_sells`、虚拟占资的短期跟踪；仍须持续用实时柜台状态对账，不能将这些跟踪演化为跨 K 交易意图。
15. 使用单线程 SDK 事件循环的 schedule 适配器（包括 GM schedule、IB `ib.sleep()` 主循环）不得在 SDK 回调线程同步执行包含 SELL 等待、现金同步和最终 BUY 的完整实盘运行；必须将当前 slot 调度到短生命周期工作线程，让订单拒单/成交回调持续由 SDK 事件循环处理。同一适配器同时最多执行一个实盘运行，重叠 slot 记录并跳过；回测/优化保持同步快速路径。
16. GM 适配器只交易中国市场，实盘调仓的 BUY/SELL 均应使用 `OrderType_Market`；BUY 的 `price` 至少覆盖实时最优卖价，SELL 使用实时行情作为保护价。该字段是市价保护价，不是限价委托价格；资金预检查、虚拟占资和拒单退款必须按实际保护价统一估算。不要把最新成交价微调后设置为 `OrderType_Limit` BUY/SELL，否则盘口偏离时会在柜台滞留。
17. 配置边界：broker-specific 的局部参数应在 adapter 内提供安全默认值，不作为 CLI 公共配置；只有稳定、跨模块复用的公开配置才进入 `config.py`。不得为单个 broker、一次性场景或兼容别名扩充 `config.py`。

---

## 📤 输出要求
- 请输出一个完整的 Python 文件代码，文件名约定为 `[broker_name]_broker.py`。
- 文件中应同时包含: `Broker`、`OrderProxy`、`DataProvider bridge`。
- 必须包含清晰的 Docstring，解释关键的参数转换逻辑。
- 仅输出代码本身及必要的逻辑说明，严格遵守上述接口签名。

开始生成：
