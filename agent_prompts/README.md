# QuantAda Agent Prompts

本目录用于“复制即用”的 agent/codegen Prompt 模板，目标是降低接口对接与命令编排成本。

重要说明:
1. `docs/specs/*` 是更正式的规范层。
2. `agent_prompts/*` 是生成模板层，不应单独充当最终契约来源。
3. 若模板、spec 与代码/tests 不一致，应以当前代码/tests 为准，并在同一变更中同步回写 spec 与模板。

## 目录结构
- `broker/BROKER_PROMPT.md`: 新券商适配器生成
- `strategy/STRATEGY_PROMPT.md`: 新策略生成
- `command/COMMAND_PROMPT.md`: 自动生成可执行 `run.py` 命令（回测/优化/实盘）
- `data_provider/DATA_PROVIDER_PROMPT.md`: 新数据源适配器生成
- `selector/SELECTOR_PROMPT.md`: 新选股器生成
- `risk_control/RISK_CONTROL_PROMPT.md`: 新风控模块生成
- `alarm/ALARM_PROMPT.md`: 新报警通道适配器生成
- `metric/METRIC_PROMPT.md`: 新优化评分函数生成（用于 `--metric`）
- `recorder/RECORDER_PROMPT.md`: 新 Recorder 生成（交易与绩效落库/落消息）
- `sdk_plugin/SDK_PLUGIN_PROMPT.md`: 外部项目插件化接入与命令生成
- `debug_fix/DEBUG_FIX_PROMPT.md`: 基于命令+日志的定位修复模板
- `research_report/PROFESSIONAL_RESEARCH_REPORT_PROMPT.md`: 专业研报、技术白皮书、合作备忘录生成模板

## 使用建议
1. 先选最贴近任务的子目录和 Prompt 文件。
2. 按模板填写输入区块（目标、参数、日志、约束）。
3. 把完整文本发给 AI，然后让 AI 直接改代码并验证。
4. 最后用 `debug_fix/DEBUG_FIX_PROMPT.md` 做回归和稳定性检查。

## 当前框架行为基线（2026-04）
1. Broker/Engine 已切换为**无状态执行**：不再使用 `deferred`/`buffered` 队列保存历史买入意图。
2. 买单拒绝后在同回调内**当场降级重提**（默认最多 10 次：前 5 次按 `LOT_SIZE` 线性降级，后 5 次按几何降级）；达到上限后放弃本 K，下一根 K 重新决策。
3. 卖出侧遵循可卖仓位约束（A 股等 T+1 场景应基于 `sellable`/`available_now` 等字段），避免“仓位不足”反复拒单。
4. 适配器层禁止维护本地资金/仓位缓存；实时状态必须以柜台查询为准。
5. 实盘每个自然日首次 `run` 会在**拉数据前**执行隔夜在途委托清理（可用 `config.KEEP_OVERNIGHT_ORDERS` 保留隔夜单）。
6. 隔夜清理失败会最多重试 5 次；若仍未清空，在继续本轮执行前会记录详细日志并推送 ERROR 级别报警。
7. `get_pending_orders` 统一契约要求包含 `id` 字段，并由适配器实现 `cancel_pending_order(order_id)` 支持按单撤单。
8. 策略侧当前的等权调仓接口为 `execute_rebalance(target_symbols, top_k, rebalance_threshold)`；`target_symbols` 传 `data` 对象列表，不传权重字典。调仓时点统一使用 `rebalance_when`：固定频率可用 `bar/daily/weekly/monthly`，不定期正式调仓可用 `next/skip`。
9. 策略交易循环直接遍历 `self.broker.datas`。
10. 实盘 adapter 模块需在同一文件中同时暴露 Broker 与 DataProvider 类，供 `LiveTrader` 反射发现。
11. 风控支持逗号分隔的多模块链式加载；`risk_params` 可为平铺 dict，也可为 `{risk_name: {...}}` 的 scoped 结构。
12. 实盘引擎自愈基线：当轮 live data refresh 不完整会跳过执行；`datas` 为空会尝试恢复；僵尸 `strategy.order` 会自动清锁。
13. live data refresh 不完整时，可在同一轮内做有限次重试；重试仍失败才跳过并告警。
14. GM/IB 的 schedule 运行支持 prewarm；相关生成/修复应保留 `LIVE_SCHEDULE_PREWARM_LEAD` 语义。
15. schedule 附近的 IM 报警支持时间窗；默认用 `LIVE_SCHEDULE_ALARM_WINDOW`，连接配置中的 `alarm_window` 可按连接覆盖。
16. 初次 `STARTED` 必须在 worker 进入 broker SDK 前发送，并在同一 worker 进程内去重；定时 `ALIVE` 生命周期消息与显式 `plan` 标签消息默认绕过时间窗；新增报警语义时优先复用 `BaseAlarm` 中的标签常量。受监督 worker 内部重启和终止不推 `STOPPED` / `DEAD`；只有操作者 `SIGINT` 安全退出才由 worker 推送一次 `STOPPED`。`1d` schedule 在每个自然日正式 slot 前 30 分钟固定推送一次仅表示 worker 存活的 `ALIVE`，非日线不发送。
17. 核心/基础层需要运行期通知时通过 `common.runtime_notifications`，不要在 rebalancer、executor、strategy/base broker 等模块里直接导入 `AlarmManager`。
18. `PRINT_PLAN=True` 时，live 运行可即时推送每次计划和策略排名快照；backtest 运行必须只在回测结束时按快照 key 推送最后一条计划/排名，并附带本次执行命令、交易归因和最终绩效摘要，本地日志可继续打印每次计划，避免历史区间触发 IM 限流。
19. `ALARMS_ENABLED=None` 为自动模式: 有任一 webhook 时启用报警通道，无 webhook 时不启用；显式 `False` 可强制禁用。`LOG` 只控制本地详细日志，不作为 IM 总开关。
20. 已知券商维护型连接失败仅记录日志并自愈，不直接推异常 IM；schedule slot 内仍会阻断执行的错误不得永久静默，并应按当前 slot 去重。schedule 告警按自然日执行，不按星期筛选，默认覆盖 7x24 时段。
21. 调仓执行器若遇到订单同步提交失败并返回 `None`，必须打印并推送 ERROR 告警；卖单失败时跳过本轮后续买入，避免“实盘信号”误导为实际委托。
22. 实盘调仓卖单等待与滚动买入只适用于 live broker；回测按计划同步执行。正常场景应优先等待 SELL 撮合后一次性买入，滚动 BUY 只在卖单等待达到告警阈值且柜台仍明确存在 SELL 在途时作为低频兜底，本轮等待内已提交过滚动买入后不得继续追加滚动单。本轮带 ID 的卖单若可信柜台在途单连续为空且只剩本地 `_pending_sells` 标记，可按终态回调滞后清理本轮 pending。SELL 清空后的最终补齐不能仅因本轮部分滚动 BUY 已出现在可信柜台 pending 快照中而整单跳过；若滚动 BUY 尚未覆盖目标，应继续按目标市值提交差额。
23. 实盘的 pending 查询若失败、断连或快照不完整，必须用短生命周期健康标记显式告知框架；该标记只用于本轮可信度判断，不得保存为状态，也不得用于回测路径。回测必须假定计划订单同步执行，保持快速流畅。
24. 任何新增执行链路都要在入口边界严格区分 live/backtest：live 只允许本轮内有限轮询、对账和自愈；回测与优化不得因为健康标记、实时 pending、现金结算或 broker 同步而阻塞或降速。
25. 优化器 MainEval 主回测至少覆盖最近 3 年；如果训练+测试逻辑窗口更长，则覆盖完整训练+测试逻辑窗口。MainEval 与年度固定窗口应优先复用本轮已加载的 `raw_datas` 切片，只有 MainEval 初始请求窗口确实未覆盖目标窗口时才允许补拉数据。
26. 横截面排名策略需要推送分数排名时，调用 `self.publish_rankings(ranked_candidates, title="ranked_symbols", dt=current_dt)`；不要在策略里直接导入 `AlarmManager`。

## 推荐阅读顺序
1. 先读 `docs/specs/*` 中与你任务最相关的正式规范。
2. 再选本目录下最贴近任务的 Prompt 模板。
3. 最后打开对应的基类接口与当前实现/测试，确保生成结果贴合真实代码。
