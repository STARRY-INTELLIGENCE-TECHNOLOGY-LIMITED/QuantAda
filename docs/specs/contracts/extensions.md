# Extension Contracts

本文件覆盖 selector / risk / data provider / alarm / recorder 的当前扩展约定。

## 1. Selector
1. 继承 `stock_selectors.base_selector.BaseSelector`
2. 核心方法: `run_selection()`
3. 返回值:
- `list[str]`
- 或以 symbol 为 index 的 `pandas.DataFrame`
4. 不在 selector 内部下单，不调用 broker 发单
5. 可使用 `self.data_manager.get_data(...)`

## 2. Risk Control
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

## 3. Data Provider
1. 继承 `data_providers.base_provider.BaseDataProvider`
2. 核心方法: `get_data(symbol, start_date, end_date, timeframe, compression)`
3. 必须提供 `PRIORITY`
4. 返回 DataFrame 要求:
- 包含 `open/high/low/close/volume`
- 时间索引为 `DatetimeIndex`
- 升序、去重
- 失败时返回 `None`
5. DataManager 支持单个或多个 `data_source` 名称，多个 provider 可按逗号或空格分隔

## 4. Alarm
1. 继承 `alarms.base_alarm.BaseAlarm`
2. 关键方法:
- `push_text`
- `push_exception`
- `push_trade`
- `push_status`
3. 失败不得抛出未捕获异常，避免影响交易主流程

## 5. Recorder
1. 继承 `recorders.base_recorder.BaseRecorder`
2. 关键方法:
- `log_trade(...)`
- `finish_execution(...)`
3. 单个 recorder 失败不应中断主流程

## 6. Backtest Plot Scope
1. `plot_scope` 是回测图表展示范围的统一入口。
2. 可用范围包括 `full`、`portfolio`、`portfolio_equity`、`portfolio_drawdown`、`monthly_heatmap`。
3. `full` 不能与其他范围混用；其余范围可用逗号组合，并复用同一次回测结果打开多个窗口。
4. 新增图表范围不得改变策略决策、撮合、优化目标或实盘路径。
