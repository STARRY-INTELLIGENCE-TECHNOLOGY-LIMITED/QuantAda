# Strategy Contract

本文件覆盖 `strategies/base_strategy.py` 及当前策略层约定。

## 1. Base Contract
1. 策略必须继承 `BaseStrategy`
2. 参数通过类属性 `params = {...}` 暴露
3. 运行时通过 `self.p.xxx` 读取参数
4. 生命周期核心方法:
- `init()`
- `next()`

## 2. Stateless Constraints
1. 不在策略内部维护虚拟现金、虚拟仓位、跨 K 买入意图队列。
2. 不设计“本 bar 卖出、下个回调强制补买”的 replay 状态机。
3. 若本 bar 买不进，交给下一根 K 重新生成目标。

## 3. Trading Universe Contract
1. 发单逻辑直接遍历 `self.broker.datas`
2. 如果只是做只读预计算，也遍历 `self.broker.datas`

## 4. Indicator Cache Contract
1. 框架可在回测/优化器中复用纯只读指标序列，主要用于重复 trial 的计算加速。
2. 策略作者只需正常调用 `register_indicator()`；不得直接依赖底层缓存字典来保证策略正确性。
3. 只允许缓存由行情数据和参数决定的指标结果，例如 MA、ROC、趋势分、布尔信号序列。
4. 不允许缓存现金、持仓、订单、目标标的、拒单重试、跨 K 买入意图或任何 broker 现实状态。
5. 实盘不得依赖该缓存维持正确性；缺少缓存时策略行为必须保持一致。
6. 缓存实现细节属于 `common/indicator_cache.py`；`BaseStrategy` 只保留 `register_indicator()`、`get_indicator()` 等稳定策略 API 入口。

## 5. Supported Trading Paradigms
1. Arbitrary target / signal-driven:
- `self.broker.order_target_percent(data, target_pct)`
- `self.broker.order_target_value(data, target_value)`
2. Equal-weight rebalance:
- `self.execute_rebalance(target_symbols, top_k, rebalance_threshold)`

## 6. Current Rebalance Semantics
1. `execute_rebalance()` 当前是等权接口，不是权重字典接口。
2. `target_symbols` 传 `data` 对象列表，不传 symbol 字符串。
3. `top_k` 代表目标持仓槽位数。
4. 需要不等权目标时，应改用 `order_target_percent/value`。

## 7. Rebalance Timing Gate
1. `execute_rebalance()` 使用统一的调仓时点入口 `rebalance_when`。
2. 若未配置 `params['rebalance_when']`，则保持旧行为: 每个策略周期都可执行。
3. `rebalance_when` 支持两类值:
- 固定频率字符串: `bar` / `daily` / `weekly` / `monthly`
- 显式调仓字符串: `next` / `skip`
4. 当 `rebalance_when='next'` 时，表示“本次就是 next rebalance”，允许把闲置资金纳入正式补仓。
5. 当 `rebalance_when='skip'` 时，表示“本次只是普通运行”，不执行正式调仓。
6. 该门控必须保持无状态:
- 不记录“上次调仓日期”
- 不维护跨 K 调仓意图
- 仅基于当前 bar 与上一 bar 的日/周/月边界判断是否到达正式调仓时点
7. 该门控用于解耦“策略运行频率”和“正式调仓频率”。

## 8. Isolated Capital Semantics
1. 策略调仓使用真实持仓 + 在途订单做 bottom-up 盘点。
2. 若 broker 提供 `get_rebalance_cash()`，策略计划口径优先使用该值。
