# 策略契约

本文件覆盖 `strategies/base_strategy.py` 及当前策略层约定。

## 1. 基础契约
1. 策略必须继承 `BaseStrategy`
2. 参数通过类属性 `params = {...}` 暴露
3. 运行时通过 `self.p.xxx` 读取参数
4. 生命周期核心方法:
- `init()`
- `next()`

## 2. 无状态约束
1. 不在策略内部维护虚拟现金、虚拟仓位、跨 K 买入意图队列。
2. 不设计“本 bar 卖出、下个回调强制补买”的 replay 状态机。
3. 若本 bar 买不进，交给下一根 K 重新生成目标。

## 3. 交易池契约
1. `self.broker.datas` 是本策略加载的标的池，策略选股、排名和发单应优先遍历它。
2. `execute_rebalance()` 只会解析 `broker.datas` 中的目标；无法解析的标的池外标的会被跳过并记录 warning，不会对其发单。若目标与池内标的仅已知 IBKR venue 后缀不同（如 `AAPL.ARCA` 与 `AAPL.SMART`），则保留兼容映射，推送 WARNING 级 IM 后继续执行本轮计划。`HK.00700`、`SHSE.600519` 这类市场前缀代码必须精确匹配，不得把 `HK` / `SHSE` 当成共享别名。
3. 账户中未出现在 `broker.datas` 标的池的持仓默认不属于本策略管理范围，不参与资金盘点，也不会因轮动目标变化被清仓；无需在 `config.py` 增加忽略列表。

## 4. 指标缓存契约
1. 框架可在回测/优化器中复用纯只读指标序列，主要用于重复 trial 的计算加速。
2. 策略作者只需正常调用 `register_indicator()`；不得直接依赖底层缓存字典来保证策略正确性。
3. 只允许缓存由行情数据和参数决定的指标结果，例如 MA、ROC、趋势分、布尔信号序列。
4. 不允许缓存现金、持仓、订单、目标标的、拒单重试、跨 K 买入意图或任何 broker 现实状态。
5. 实盘不得依赖该缓存维持正确性；缺少缓存时策略行为必须保持一致。
6. 优化器指标缓存是有界缓存，允许按 LRU 淘汰旧序列；策略正确性不得依赖缓存命中。
7. 缓存实现细节属于 `common/indicator_cache.py`；`BaseStrategy` 只保留 `register_indicator()`、`get_indicator()` 等稳定策略 API 入口。
8. 实盘引擎会原地替换 `data.p.dataname`。只在 `init()` 预计算的指标序列会在后续 live refresh 后过期；必须在 `next()` 按当前 DataFrame 重算，或按行情内容失效缓存。

## 5. 支持的交易范式
1. Arbitrary target / signal-driven:
- `self.broker.order_target_percent(data, target_pct)`
- `self.broker.order_target_value(data, target_value)`
2. Equal-weight rebalance:
- `self.execute_rebalance(target_symbols, top_k, rebalance_threshold)`
3. 目标仓位金额按 `price × contract_multiplier` 解释；普通股票乘数为 1，衍生品 adapter 可通过 `get_contract_multiplier(data)` 提供真实名义乘数。

## 6. 当前调仓语义
1. `execute_rebalance()` 当前是等权接口，不是权重字典接口。
2. `target_symbols` 传 `data` 对象列表，不传 symbol 字符串。
3. `top_k` 代表目标持仓槽位数。若解析后的目标多于 `top_k`，按传入顺序截断并告警，避免按 `capital/top_k` 给额外标的超配。
4. 需要不等权目标时，应改用 `order_target_percent/value`。

## 7. 调仓时点门控
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

## 8. 独立资金语义
1. 策略调仓使用真实持仓 + 在途订单做 bottom-up 盘点。
2. 若 broker 提供 `get_rebalance_cash()`，策略计划口径优先使用该值。
3. 若 broker 提供 `get_rebalance_position_value(data, signed_size, price, market_value)`，
   策略使用其结果参与资金盘点；该入口用于适配期权担保/保证金口径，BaseStrategy 不识别具体策略名称或券商规则。

## 9. 策略排名通知
1. 横截面排名/轮动策略需要推送分数排名时，使用 `self.publish_rankings(ranked_candidates, title="ranked_symbols", dt=current_dt)`。
2. `ranked_candidates` 推荐传 `[(data, score), ...]`，其中 `data` 是当前 broker 管理的数据对象。
3. 策略不得直接导入 `AlarmManager` 推送排名；通知分发通过 `common.runtime_notifications` 边界完成。
4. `PRINT_PLAN=True` 时，live 模式即时推送排名；backtest 模式只保留最后一条排名快照并在回测结束时统一推送，回测结束时还会附带执行命令、交易归因和最终绩效摘要。

## 10. 期权标的池展开
1. 股票/ETF 仍使用静态 `--symbols` 或 selector 一次输出的代码列表。
2. 期权策略若要把标的池展开为滚动合约，应在策略类上声明 `option_universe`，例如 `option_universe = ("PUT",)` 或 `True`。
3. 未声明时，运行时不得自动把正股代码展开成期权链，避免股票策略误拉全链。
4. 展开过滤读取策略 `params` 的 `min_dte`/`max_dte`，以及 `min_delta`/`max_delta`/`protective_put_delta` 的并集；缺少 DTE 窗口时失败关闭。
5. 回测/优化在取数前按 as_of 抽样历史链并求并集，必须使用 ThetaData 或 `theta+futu` 的历史链，禁止用 Futu 当前链回放。并集应按时间均匀封顶，不得只收下窗口开头的合约，否则 Optuna 训练集/测试集会没有可交易期权。展开和合约历史拉取应周期性打印进度（当前 as_of、成功数、空结果数、失败数、合约数、已用时间）。Theta 的 No data found 不逐条打印、不重试；超时与瞬时错误有界重试 5 次，失败 as_of 与漏拉合约在本轮扫完后补偿一轮。`CACHE_DATA=True` 时按 as_of 断点续拉；`--refresh` 忽略断点并全量重拉。实盘不写该断点。
历史链候选若声明 `protective_put_delta`，每个 as_of 必须同时覆盖 Short Put 的中心 Delta 和保护腿 Delta；断点缓存身份必须包含 Delta 锚点，不能复用只围绕短腿中心生成的旧缓存。
6. 实盘每个 schedule slot 用当前链增补合约；账户已有持仓或在途的旧合约必须保留，即使它们还不在当前 datas。pending 快照不可信，或持仓查询异常时，不得把现有期权 feed 当作空仓丢弃。
7. 策略仍只交易 `self.broker.datas` 中的对象，并在 `next()` 按当前 DTE/Delta 再过滤；不要缓存 init 时的合约列表。已有持仓或在途期权即使当前 K 线被零填充、没有可交易报价，也必须占用底层名额；保护腿缺报价时不得把它当成已移除后单独买平空头。
8. 框架内置期权样例位于 `strategies/options/`，覆盖买开 Put/Call、现金担保短 Put、Covered Call 和原子 Put Credit Spread。运行使用全限定类名；各样例文件顶部有可复制的 `run.py` 命令。裸卖、Call 价差和未实现多腿组合必须失败关闭，不要在样例里顺序拆腿。
