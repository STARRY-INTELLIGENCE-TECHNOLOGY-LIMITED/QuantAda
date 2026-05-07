# Live Runtime Semantics

本文件描述 QuantAda 当前实盘引擎的重要运行语义。

## 1. Live Run High-Level Order
1. `broker.set_datetime(context.now)`
2. 若为实盘:
- 在拉数前执行隔夜在途委托清理，除非 `KEEP_OVERNIGHT_ORDERS=True`
3. 刷新 live data
4. 若刷新不完整，跳过本轮执行并告警
5. 若当前无可交易数据，尝试恢复 data feeds
6. 先执行风控检查
7. 检查并自愈僵尸 `strategy.order`
8. 若仍有 pending order，通知并跳过策略逻辑
9. 执行 `strategy.next()`

## 2. BUY Rejection Semantics
1. BUY 拒单由 `BaseLiveBroker.on_order_status()` 统一处理。
2. 语义为“无状态 + 当场重提”。
3. 默认最多 10 次:
- 前 5 次按 `LOT_SIZE` 线性降级
- 后 5 次按几何降级
4. 达到上限后放弃本 K，等待下一根 K 重新决策。
5. 多标的拒单重试必须互相独立。

## 3. SELL Semantics
1. 卖出受 `sellable` / `available_now` / `available` 等可卖字段约束。
2. T+1 市场下，有持仓但不可卖时，直接跳过卖单，避免反复“仓位不足”拒单。
3. 调仓执行遵循先卖后买。
4. 若清仓/减仓卖单同步提交失败并返回 `None`，必须打印并推送 ERROR 告警，且跳过本轮计划中的后续买入。
5. 若买单同步提交失败并返回 `None`，必须打印并推送 ERROR 告警，避免只有“实盘信号”日志而无实际委托。

## 4. Overnight Pending Order Cleanup
1. 默认在每个自然日首次 `run()`、拉数前执行。
2. `cleanup_overnight_orders()` 失败或屏障未清空时，最多重试 5 次。
3. 若 5 次后仍未清空:
- 继续本轮执行
- 打印详细日志
- 推送 ERROR 告警
4. `KEEP_OVERNIGHT_ORDERS=True` 时跳过此流程。

## 5. Live Self-Healing Baseline
1. Live data refresh 不完整时，不执行当轮策略。
2. 在判定跳过前，可在同一轮内对 live data refresh 做有限次重试；若仍不完整，再跳过本轮并告警。
3. `datas` 为空时，尝试恢复历史数据与 data feed。
4. 若策略层残留 `strategy.order`，但柜台和 broker 内部已无在途状态，则自动清锁。
5. 风控支持多模块链式挂载。
6. GM / IB schedule 运行支持 prewarm；相关改动不得破坏 `LIVE_SCHEDULE_PREWARM_LEAD` 语义。
7. schedule 附近的 IM 报警推送支持时间窗限制；默认读取 `LIVE_SCHEDULE_ALARM_WINDOW`，连接配置中的 `alarm_window` 可覆盖全局默认值。
8. `STARTED` / `STOPPED` / `DEAD` 等生命周期消息，以及显式标注为 `plan` 的执行计划消息，不受 schedule 报警时间窗限制。
9. 实盘阻断类错误告警不得在长进程内永久静默；若按 schedule 去重，应以当前 schedule slot 为作用域（如 `1d` 每日、`5m` 每 5 分钟 slot）。
