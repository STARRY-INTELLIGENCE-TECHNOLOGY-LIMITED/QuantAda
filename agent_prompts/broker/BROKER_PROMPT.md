# QuantAda Framework - 券商适配器 (Broker Adapter) AI 生成指令

## 🤖 系统角色定义 (System Role)
你现在是一位拥有 10 年经验的企业级量化交易系统架构师。你需要为一个名为 **QuantAda** 的开源全天候量化交易框架编写一个新的底层券商适配器（Broker Adapter）。
请仔细阅读以下【输入信息】与【接口契约】，并严格生成健壮、符合类型提示（Type Hints）的 Python 代码。

---

## 📥 输入信息 (Inputs)
- **目标券商名称**: [请在此处填入券商名称，例如：Longbridge / Futubull / Charles Schwab]
- **目标券商 API 文档**: [请在此处粘贴或上传该券商的官方 Python SDK 文档、发单接口、资产查询接口、状态流转等]

---

## 🏛️ 核心架构约束 (Architecture Constraints)

1. **继承基类**: 你的主类必须命名为 `[BrokerName]Broker`，并且严格继承自 `live_trader.adapters.base_broker.BaseLiveBroker`。
2. **模块装载契约**: `live_trader.engine.LiveTrader` 会在同一个 adapter 模块中同时反射 Broker 和 DataProvider。因此输出文件中除了 `[BrokerName]Broker` 外，还必须同时暴露一个 `BaseDataProvider` 子类（可为薄封装），并兼容 `get_history(...)` 调用。
3. **绝对无状态 (Stateless)**: QuantAda 已移除 `deferred/buffered` 买单队列。适配器内部**严禁**维护任何类似 `self.local_cash` 或 `self.local_positions` 的缓存变量，也**严禁**自行实现跨回调重试队列。所有状态查询必须实时通过 API 向物理柜台发起。
4. **数据对象解包**: 框架传入的 `data` 参数是一个代理对象（DataFeedProxy）。获取标的代码时，必须使用 `data._name`，并在与券商 API 交互前，根据需要进行格式化（例如截取基础代码 `data._name.split('.')[0].upper()`）。
5. **卖出可用仓位约束**: 对存在 T+1 或可卖冻结语义的市场，必须提供准确可卖仓位（建议实现/覆盖 `get_sellable_position`），不要仅用总仓位代替可卖仓位。

---

## 🛑 必须实现的接口契约 (Required Interface Contracts)

你必须严格实现以下 `@abstractmethod`，严禁修改方法签名：

### 1. 资产与持仓查询
- `getvalue(self) -> float`: 获取当前账户总权益（Net Liquidation Value）。可调用父类的 `self._get_portfolio_nav()` 或直接调用券商 API 获取。
- `_fetch_real_cash(self) -> float`: 实时向柜台请求当前可用于开新仓的真实购买力（现金）。如券商口径不含在途冻结，需在适配器层补充扣减逻辑。
- `get_position(self, data)`: 获取指定标的持仓。必须返回一个拥有 `.size` (持仓数量) 和 `.price` (成本价) 属性的对象（可使用 `SimpleNamespace` 模拟）。若市场有可卖限制，建议同时暴露 `.sellable`。
- `get_sellable_position(self, data)`（建议覆盖）: 返回当前真实可卖仓位；若不覆盖，基类会退化为 `size`。
- `get_current_price(self, data) -> float`: 获取指定标的实时盘口价或最新快照价。若获取失败、断流或停牌，必须安全返回 `0.0`，严禁抛出异常。

### 2. 订单系统
- `get_pending_orders(self) -> list`: 获取所有未完成的在途订单。**必须返回以下严格格式的字典列表**：
  `[{'id': '123', 'symbol': 'AAPL', 'direction': 'BUY' 或 'SELL', 'size': 100}, ...]`
- `cancel_pending_order(self, order_id: str) -> bool`: 按订单ID发起撤单。返回是否成功发起撤单请求（True/False）。该接口用于引擎在交易日首轮前清理隔夜在途单。
- `_submit_order(self, data, volume: int, side: str, price: float)`: 核心发单路由。`side` 为 `'BUY'` 或 `'SELL'`。将其翻译为目标券商的结构体并发起发单请求，发单成功后返回自定义的 `BaseOrderProxy` 子类实例，失败返回 `None`。

### 3. 状态转换器与代理类
- **必须创建一个子类**继承自 `live_trader.adapters.base_broker.BaseOrderProxy`，并实现其所有的 `@abstractmethod` 属性和方法（包括 `is_accepted()`）。
- **当前运行时还要求代理对象暴露以下属性/字段**:
  - `status`: 原始或标准化后的订单状态
  - `executed`: 一个带 `size`, `price`, `value`, `comm`，并最好带 `dt` 的对象
  - `data`: 匹配到的框架 data 对象（匹配失败时可为 `None`）
- `id` 必须稳定且可用于后续撤单；若券商原生 `orderId` 可能缺失，应提供可区分的兜底标识。
- `convert_order_proxy(self, raw_order) -> BaseOrderProxy`: 引擎回调入口。将目标券商特有的 Trade/Order 回调对象，解析并转换为上述自定义的 `BaseOrderProxy` 对象。**注意：匹配归属的 data 对象时，严禁使用 `in` 进行模糊匹配，必须使用精确的字符串等于判定。**

### 4. 运行环境适配
- `@staticmethod` `is_live_mode(context) -> bool`: 判断当前上下文是否为实盘模式。
- `@classmethod` `launch(cls, conn_cfg: dict, strategy_path: str, params: dict, **kwargs)`: [可选实现] 命令行实盘启动入口，负责初始化券商 SDK、建立连接并挂载事件循环。
- `DataProvider` 子类: 必须让引擎能通过当前 adapter 模块直接发现；如果历史数据能力来自现有 provider，也请在本文件中提供桥接类，而不是只写说明文字。

---

## ⚙️ 与当前框架一致的执行语义 (必须遵守)
1. 买单拒绝后的降级重提由 `BaseLiveBroker.on_order_status` 统一处理（默认最多 10 次：前 5 次 `LOT_SIZE` 阶梯降级 + 后 5 次几何降级）；适配器不要额外叠加自己的“拒单队列”。
2. 禁止实现或依赖以下旧机制: `process_deferred_orders`、`reconcile_buffered_retries`、`_deferred_orders`、`_buffered_rejected_retries`。
3. 若券商返回 `Inactive/Cancelled/Rejected` 语义有差异，必须在 `BaseOrderProxy` 中准确映射，否则会破坏统一降级流程。
4. 引擎会在实盘每个自然日首次 `run`、拉数据前尝试清理隔夜在途单（由 `config.KEEP_OVERNIGHT_ORDERS` 控制）。适配器必须保证:
- `get_pending_orders` 中 `id` 可用于撤单
- `cancel_pending_order` 幂等、异常安全（失败返回 False，不抛出致命异常）
5. 当前拒单重试语义为“无状态 + 当场重提”: 前 5 次按 `LOT_SIZE` 线性降级，后 5 次按几何倍数降级；适配器侧必须提供真实现金口径，避免重试阶段出现系统性偏差。
6. 当实盘 schedule 期间券商平台未启动、API 不可用或连接失败时，需在 prewarm 与实际 run 时刻分别推送 slot 级 ERROR 告警，但不得把该 slot 误记为已执行。

---

## 📤 输出要求 (Output Format)
- 请输出一个完整的 Python 文件代码，文件名约定为 `[broker_name]_broker.py`。
- 文件中应同时包含: `Broker`、`OrderProxy`、`DataProvider bridge`。
- 必须包含清晰的 Docstring，解释关键的参数转换逻辑。
- 仅输出代码本身及必要的逻辑说明，严格遵守上述接口签名。

开始生成：
