# 扩展契约

本文件覆盖 selector / risk / data provider / alarm / recorder 的当前扩展约定。

## 1. 选股器
1. 继承 `stock_selectors.base_selector.BaseSelector`
2. 核心方法: `run_selection()`
3. 返回值:
- `list[str]`
- 或以 symbol 为 index 的 `pandas.DataFrame`
4. 不在 selector 内部下单，不调用 broker 发单
5. 可使用 `self.data_manager.get_data(...)`

## 2. 风控模块
1. 继承 `risk_controls.base_risk_control.BaseRiskControl`
2. 核心方法: `check(data) -> str`
3. 返回 `'SELL'` 表示触发卖出，其余返回视为不动作
4. 可选实现:
- `notify_order(order)`
- `notify_trade(trade)`
5. 当前引擎支持逗号分隔的多风控链式加载
6. `risk_params` 可为:
- 平铺 dict
- `{risk_name: {...}}` scoped dict

## 3. 数据源
1. 继承 `data_providers.base_provider.BaseDataProvider`
2. 核心方法: `get_data(symbol, start_date, end_date, timeframe, compression)`
3. 必须提供 `PRIORITY`
4. 返回 DataFrame 要求:
- 包含 `open/high/low/close/volume`
- 时间索引为 `DatetimeIndex`
- 升序、去重
- 失败时返回 `None`
5. DataManager 支持单个或多个 `data_source` 名称，多个 provider 可按逗号或空格分隔
6. 日内 provider 必须同时按能力支持 `timeframe='Minutes'|'Seconds'` 与 `compression`；日期参数在日内模式应保留到秒，增量窗口不得擅自扩成整年高频明细。
7. SDK/网络调用必须使用有限超时；秒级请求的单次超时应明显短于数据周期。24x7 数据源不得强制应用常规交易时段过滤。

## 4. 报警通道
1. 继承 `alarms.base_alarm.BaseAlarm`
2. 关键方法:
- `push_text`
- `push_exception`
- `push_trade`
- `push_status`
3. 失败不得抛出未捕获异常，避免影响交易主流程
4. 调仓、执行器、策略基类、broker 基类等核心/基础层不得直接导入具体 IM manager；需要运行期通知时通过 `common.runtime_notifications` 发出通知意图，由 `alarms` 包负责具体通道。
5. `PRINT_PLAN=True` 时，live 运行可即时推送每次计划及策略排名快照；backtest 运行必须只在回测结束时按快照 key 推送最后一条计划/排名，并在报警通道启用时附带本次执行命令、交易归因和最终绩效摘要，本地日志可继续打印每次计划，避免历史区间触发 IM 限流。
6. `ALARMS_ENABLED=None` 为自动模式: 有任一 webhook 时启用报警通道，无 webhook 时不启用；显式 `False` 用于强制禁用。
7. `LOG` 只控制本地详细日志，不作为 IM 推送总开关。

## 5. 记录器
1. 继承 `recorders.base_recorder.BaseRecorder`
2. 关键方法:
- `log_trade(...)`
- `finish_execution(...)`
3. 单个 recorder 失败不应中断主流程

## 6. 回测绘图范围
1. `plot_scope` 是回测图表展示范围的统一入口。
2. 可用范围包括 `full`、`portfolio`、`portfolio_equity`、`portfolio_drawdown`、`monthly_heatmap`。
3. `full` 不能与其他范围混用；其余范围可用逗号组合，并复用同一次回测结果打开多个窗口。
4. 新增图表范围不得改变策略决策、撮合、优化目标或实盘路径。
