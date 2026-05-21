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
4. `common.rebalancer` 只负责调仓计划与仓位平衡计算；实盘发单、卖单等待和滚动买入逻辑属于 `common.order_executor`。
5. 卖单等待以柜台在途单和本地 `_pending_sells` 为主；若终态回调缺失但实时持仓已达到本轮卖出目标，可清理本轮本地 pending 标记并同步资金。
6. 卖单等待期间允许用实时可用现金滚动释放后续买单，已经释放但尚未被持仓/在途买单确认的部分不得重复提交。
7. 未确认卖出的剩余资金必须继续保守；只有 SELL 在途状态清空后，才允许对计划内剩余买单做最终补齐。
8. 卖单等待必须有硬上限，硬等待后必须返回当前 `run()`，不得无限阻塞 live schedule 后续触发。
9. 若硬等待后仍存在 SELL 在途，必须打印并推送 ERROR 告警；本轮只保留已经由实时现金确认并滚动释放的买单，不得全量放行。
10. 上述持仓/现金一致性兜底只用于当前调仓等待流程，不得保存或重放历史买入意图。
11. 若清仓/减仓卖单同步提交失败并返回 `None`，必须打印并推送 ERROR 告警，且跳过本轮计划中的后续买入。
12. 若买单同步提交失败并返回 `None`，必须打印并推送 ERROR 告警，避免只有“实盘信号”日志而无实际委托。

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
8. 使用实盘 schedule 回调的 adapter 必须在 context 上暴露 `schedule_rule` 或 `use_schedule`，使基础 broker 能区分正常调度间隔和异常长中断。
9. `STARTED` / `STOPPED` / `DEAD` 等生命周期消息，以及显式标注为 `plan` 的执行计划消息，不受 schedule 报警时间窗限制。
10. 实盘阻断类错误告警不得在长进程内永久静默；若按 schedule 去重，应以当前 schedule slot 为作用域（如 `1d` 每日、`5m` 每 5 分钟 slot）。
11. 若在 schedule prewarm 或实际 run 时刻券商平台未启动、API 不可用或连接失败，应推送 slot 级 ERROR 报警，但不得把该 slot 误记为已执行。
