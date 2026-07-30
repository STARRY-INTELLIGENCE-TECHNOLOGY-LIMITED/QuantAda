# Live Runtime Semantics

本文件描述 QuantAda 当前实盘引擎的重要运行语义。

## 1. Live Run High-Level Order
1. `broker.set_datetime(context.now)`
2. 若为实盘:
- 在隔夜清理前创建本轮共享的 monotonic execution deadline
- 在拉数前执行隔夜在途委托清理，除非 `KEEP_OVERNIGHT_ORDERS=True`
3. 刷新 live data
4. 若刷新不完整，跳过本轮执行并告警
5. 若当前无可交易数据，尝试恢复 data feeds
6. 先执行风控检查
7. 每轮实盘有界重试查询柜台 pending 快照；重试后仍异常或不可信时打印并推送 ERROR，跳过本轮
8. 检查并自愈僵尸 `strategy.order`
9. 若 `strategy.order` 仍在途，通知并跳过策略逻辑；仅存在可信的 broker pending 时继续当前策略决策，由目标下单边界把在途数量计入预期仓位，尽力完成剩余计划且避免重复数量
10. 执行 `strategy.next()`

## 2. Shared Live Run Deadline
1. 每次实盘 `run()` 从隔夜委托清理前开始共享一个单调时钟截止时间；默认总预算为 `LIVE_RUN_MAX_EXECUTION_SECONDS=600` 秒。
2. 短周期触发自动取“配置上限”和“触发间隔的 80%”中的较小值，例如 `5m` schedule 为 240 秒、`1m` schedule 为 48 秒；无明确 schedule 时按 `timeframe=Minutes|Seconds` 与 `compression` 推导，例如 5 秒 bar 为 4 秒。该机制不绑定特定市场或固定收盘时间。
3. 隔夜清理、数据刷新/恢复、pending 查询、风控遍历、SELL 等待、资金同步/等待、BUY/SELL 拆单和 BUY 降级重试都使用同一截止时间，不能把各自的局部重试上限串联成更长总耗时。
4. 截止时间到达后不得发起新的查询、撤单、拆单子单或降级重试；已经被柜台接受/成交的部分订单继续有效并保留跟踪，未完成意图不持久化、不跨 K 重放。
5. SELL 等待会从总预算中预留最终处理窗口：基准取总预算的 10%，最少 0.1 秒、最多 30 秒；若进入 SELL 等待时剩余预算已经较少，预留值再限制为当时剩余时间的一半，使 SELL 对账与最终处理都至少有机会执行。SELL 已由真实持仓确认后，现金快照等待可使用预留窗口之前的全部剩余预算，不受旧的 10 秒局部上限限制；到达预留窗口仍未更新时，停止等待并按当前可用现金尽力提交最终 BUY。若 SELL 尚未通过真实持仓确认，则跳过最终 BUY。
6. 异步 BUY 拒单只能使用原订单提交时捕获的 run deadline；旧订单回调不得借用后续 run 的新预算继续执行旧意图。
7. 回测和优化不创建、不查询该 deadline，继续保持内存内同步、非阻塞执行。
8. Python 截止时间只能阻止框架发起下一次操作，不能抢占已经阻塞在原生 SDK/网络调用中的线程；所有 live adapter 的查询、撤单、发单和同步接口必须配置有限 SDK 超时，并使单次调用明显短于本轮预算。

## 3. BUY Rejection Semantics
1. BUY 拒单由 `BaseLiveBroker.on_order_status()` 统一处理。
2. 语义为“无状态 + 当场重提”。
3. 默认最多 10 次:
- 前 5 次按 `LOT_SIZE` 线性降级
- 后 5 次按几何降级
4. 达到上限后放弃本 K，等待下一根 K 重新决策。
5. 多标的拒单重试必须互相独立。
6. 若券商 `_submit_order()` 同步返回 Rejected/Canceled/Expired 等非可继续成交状态，不能把该返回值视为已提交委托；BUY Rejected 必须立即进入同一套降级重试路径。`[BUY] [实盘信号]` 日志只能在委托被接受、在途或已成交后打印，避免出现“信号日志存在但柜台没有委托”的误判。
7. `LOT_SIZE` 与实盘 `BROKER_LOT_LIMITS` 都允许正整数或正小数，例如币市可使用 `LOT_SIZE=0.00000001`；禁止在基础层、adapter、pending 快照或日志路径中无条件 `int()` 截断订单/持仓数量。BUY 在首次提交前按单笔上限拆分并按十进制 `LOT_SIZE` 向下对齐，所有子单只在当前调用内同步提交并独立跟踪。任一子单（包括首笔）同步失败时，在当前 run deadline 内复用前 5 次减少一个 `LOT_SIZE` + 后 5 次几何降级；adapter 实际受理量小于请求量时，基础层按真实受理量记账并继续尝试剩余差额。耗尽后停止、记录并推送 ERROR，不保存剩余买入意图。`BROKER_LOT_LIMITS=0` 表示不限制，回测/优化不得进入实盘拆单路径。框架不另设隐藏的拆单笔数上限，操作者必须按券商真实单笔限制配置，避免过小值产生大量子单。
8. 柜台拒单详情必须同时写入本地日志并推送 IM，不能只存在于通知渠道；拒单文本仅用于审计，不得通过 NLP 驱动执行策略。

## 4. SELL Semantics
1. 卖出受 `sellable` / `available_now` / `available` 等可卖字段约束。
2. T+1 市场下，有持仓但不可卖时，直接跳过卖单，避免反复“仓位不足”拒单。
3. SELL 与 BUY 使用相同的正整数或正小数 `BROKER_LOT_LIMITS`，并按十进制 `LOT_SIZE` 对齐；SELL 在当前调用内同步拆单、独立跟踪所有子单，清仓时最后一笔允许保留不足 `LOT_SIZE` 的碎股。任一子单（包括首笔）同步失败、拒绝或 adapter 实际受理量小于请求量时，都在当前 run deadline 内按统一的 5 次 lot 线性 + 5 次几何降级继续尝试剩余数量。耗尽后若已有子单受理，返回首笔代理并标记批次失败；执行器有界等待并按该批实际受理总量对账。若没有子单受理则返回 `None`。不保存剩余卖出意图。`0` 表示不限制，回测/优化保持单笔同步路径。
4. 调仓执行遵循先卖后买。
5. `common.rebalancer` 只负责调仓计划与仓位平衡计算；实盘发单、卖单等待和滚动买入逻辑属于 `common.order_executor`。
6. 卖单等待以柜台在途单和本地 `_pending_sells` 为主；若终态回调缺失但实时持仓已达到本轮卖出目标，或本轮带 ID 的卖单在可信柜台在途单连续为空后只剩本地滞后标记，可清理本轮本地 pending 并同步资金。拆分卖单必须按整批订单 ID 和实际总提交量执行持仓对账与资金估算，不能只跟踪首笔子单。pending 状态清空后，最终买入前仍必须在当前硬等待范围内持续用真实持仓追认本轮每个卖出目标；持仓不可用或到硬上限仍高于目标时打印并推送 ERROR，禁止最终买入。
7. 卖单等待期间默认优先等待卖出撮合完成后一次性买入。滚动买入只是低频兜底：只有卖单等待达到告警阈值且柜台仍明确存在 SELL 在途时，才允许用实时可用现金滚动释放后续买单；本轮等待内已提交过滚动买入后不得继续追加滚动单，已经释放但尚未被持仓/在途买单确认的部分不得重复提交。
8. 未确认卖出的剩余资金必须继续保守；只有 SELL 在途状态清空后，才允许对计划内剩余买单做最终补齐。最终补齐前可做有界现金快照等待，等待目标应按本轮卖出估算释放金额、计划买入目标和容忍系数收敛；若实际到账金额与估算金额存在误差导致超时，必须告警并继续按当前可用现金提交，不能无限阻塞。若本轮已有部分滚动 BUY 仍在途且已出现在可信柜台 pending 快照中但尚未覆盖计划目标，最终补齐不得因此整单跳过，应继续通过目标市值下单让 broker 按预期仓位、可用现金和预扣资金提交差额。
9. 上述卖单等待与滚动买入只适用于实盘 broker；回测 broker 按计划顺序同步执行，不进入等待循环，不查询实时 pending，不等待现金结算，不调用实盘资金同步。
10. 卖单等待必须有硬上限，硬等待后必须返回当前 `run()`，不得无限阻塞 live schedule 后续触发。
11. 若硬等待后仍存在 SELL 在途，必须打印并推送 ERROR 告警；本轮只保留已经由实时现金确认并滚动释放的买单，不得全量放行。
12. 上述持仓/现金一致性兜底只用于当前调仓等待流程，不得保存或重放历史买入意图；pending 快照可信度标记只能作为短生命周期健康标记，不得演化为状态机或跨 K 意图缓存。
13. 清仓/减仓卖单部分受理时，必须打印并推送 ERROR 告警，有界等待已受理部分并用实时持仓确认该部分目标；确认后继续让后续 BUY 按真实现金保守截断。若已受理 SELL 尚未达到其实际提交目标、pending 不可信或 deadline 到期，禁止继续 BUY。完全未受理 SELL 时仍允许 BUY 在 broker 真实现金边界内尝试部分执行。
14. BUY/SELL 同步提交失败必须先在当前 run deadline 内降级重试；最终仍返回 `None` 时打印并推送 ERROR，避免只有“实盘信号”日志而无实际委托。
15. 新增执行逻辑在 live/backtest 两条路径上必须保持语义分离: live 允许本轮内有限轮询、对账和滚动补买；backtest 必须按计划顺序同步完成，不应因 live 健康标记、pending 快照或现金结算等待而降速或阻塞。

## 5. Overnight Pending Order Cleanup
1. 默认在每个自然日首次 `run()`、拉数前执行。
2. `cleanup_overnight_orders()` 失败或屏障未清空时，最多重试 5 次。
3. 若 5 次后仍未清空:
- 继续本轮执行
- 打印详细日志
- 推送 ERROR 告警
4. `KEEP_OVERNIGHT_ORDERS=True` 时跳过此流程；24x7 市场跨自然日时还必须保留 `_active_buys`、`_pending_sells` 与对应虚拟占资，直到柜台终态或显式恢复流程完成，不能仅保留远端订单却清空本地短期跟踪。

## 6. Intraday Data Windows
1. `Minutes` 与 `Seconds` 都按日内数据处理；首次拉取和每日重基准使用引擎内部固定的 1000 bars 有界时间窗口，不按 252/365 个自然日请求高频明细，也不增加用户配置项。
2. 增量刷新从最后一根 bar 回退 3 个周期，以覆盖延迟修订；时间参数必须保留到秒。
3. 秒级 provider 必须支持 `timeframe='Seconds'`，网络/SDK 单次超时应明显短于实际 bar/触发周期；24x7 数据请求不得强制使用仅常规交易时段过滤。

## 7. Live Self-Healing Baseline
1. Live data refresh 不完整时，不执行当轮策略。
2. 在判定跳过前，可在同一轮内对 live data refresh 做有限次重试；若仍不完整，再跳过本轮并告警。
3. `datas` 为空时，尝试恢复历史数据与 data feed。
4. 若策略层残留 `strategy.order`，但柜台和 broker 内部已无在途状态，则自动清锁。
5. 风控支持多模块链式挂载。
6. GM / IB schedule 运行支持 prewarm；相关改动不得破坏 `LIVE_SCHEDULE_PREWARM_LEAD` 语义。
7. schedule 附近的 IM 报警推送支持时间窗限制；默认读取 `LIVE_SCHEDULE_ALARM_WINDOW`，连接配置中的 `alarm_window` 可覆盖全局默认值。
8. 使用实盘 schedule 回调的 adapter 必须在 context 上暴露 `schedule_rule` 或 `use_schedule`，使基础 broker 能区分正常调度间隔和异常长中断。
9. 初次 `STARTED` 必须在 worker 进入 broker SDK 启动前发送，不能依赖 SDK init 回调；同一 worker 进程内只发送一次。定时 `ALIVE` 状态以及显式标注为 `plan` 的执行计划消息不受 schedule 报警时间窗限制；受监督 worker 的内部重启和终止不得推送 `STOPPED` / `DEAD`，监督父进程只负责日志与重启，不初始化告警通道。只有操作者发起的安全退出才由 worker 推送一次 `STOPPED`。
10. 实盘阻断类错误告警不得在长进程内永久静默；若按 schedule 去重，应以当前 schedule slot 为作用域（如 `1d` 每日、`5m` 每 5 分钟 slot）。
11. 已知券商连接维护错误仅记录日志并自愈，不直接推送异常 IM；若 schedule prewarm 或实际 run 时刻平台仍不可用，应保留按 slot 去重的 ERROR 报警，但不得把该 slot 误记为已执行。schedule 告警按自然日执行，不按星期筛选，默认覆盖 7x24 时段。
12. GM / IBKR schedule prewarm 与正式 run 都必须按目标 schedule slot 去重；重复 prewarm 回调不得重复执行或持续打印 `Prewarm Finished`。
13. 使用 SDK 事件循环的实盘 Phoenix loop 必须把 SDK 轮询/协作等待函数抛出的 `SystemExit` 视为 session 退出并重启；未标记的 `SystemExit` 应打印带时间戳日志并推送异常，避免 nohup 进程被 SDK 直接带退出。GM `gmi_poll()` 的普通非零返回值遵循官方循环语义，仅限频记录并继续驱动事件；不得将无错误消息的 `-1` 误判为 session 终止并持续重建 worker。明确 shutdown 回调、`SystemExit` 和连接健康超时仍须触发干净重启。人工 `KeyboardInterrupt` 仍应退出。
14. GM / IBKR 长进程运行期 warning/error/Phoenix 生命周期日志应通过 `common.live_runtime.runtime_print()` 带本地时间戳，便于排查夜间断线、SDK 退出和重复回调窗口。
15. GM live `gmi_init()` 失败后必须在同进程内重新绑定 token/server/strategy/callback 并尝试 SDK soft reset；若连续初始化失败达到自愈阈值，应 re-exec 当前 Python 进程，覆盖“首次终端不可用导致 SDK 状态卡死，人工重启进程才恢复”的场景。
16. 通过 `run.py --connect` 启动的实盘命令由轻量父进程监督、子进程承载策略与券商 SDK:
    - 子进程必须定期写入短生命周期 heartbeat；父进程检测进程退出、heartbeat 停滞和 adapter 上报的有界健康期限。
    - 异常退出或探活超时必须终止子进程并以原始命令冷启动，记录退出码/信号与恢复原因；重启使用有界退避。
    - 连接维护等可降噪故障必须通过结构化故障类别跨进程传递，不得解析 state、detail 或 reason 文本来决定是否推送 IM。
    - 子进程正常返回时必须显式标记 expected exit，父进程不得无条件重放；监督器不保存订单、持仓或交易意图。
    - 操作者向父子进程发送 `SIGINT` 时，父进程应转发安全退出信号，worker 推送一次 `STOPPED` 并标记 expected exit；内部探活回收继续使用 `SIGTERM` 且不推生命周期 IM。
    - 该监督链路只适用于 live connect；回测/优化不得创建监督进程、heartbeat 线程或等待循环。
17. 配置 `1d` schedule 且启用 IM 时，每个自然日必须在正式 slot 前 30 分钟推送一次 `ALIVE` 状态；该通知只表示 worker 存活，不承诺券商连接可用，不新增配置开关，非日线 schedule 不发送。该调度不按星期筛选，默认覆盖 7x24 时段。
18. GM 已知维护型连接故障采用 schedule 驱动的两档恢复，但不得硬编码交易日历或完全停掉探测：有效 `1d` schedule 在正式 slot 前 30 分钟（与 `ALIVE` 相同边界）切回积极恢复，若 `LIVE_SCHEDULE_PREWARM_LEAD` 更早则取更早边界，并保持到正式 slot 后本轮 live execution budget 结束；边界外 `gmi_init` 至少每 10 分钟真实探测一次，最后一次等待必须截断到恢复边界。固定间隔 schedule 的恢复提前量至少覆盖“调度间隔减去本轮 execution budget”的尾部，因此当天首个 anchor 后的连续高频 slot 之间不得出现恢复盲区，首个 anchor 前的安静等待也不得跨过该恢复边界；无有效 schedule 时保持积极恢复。GM 1100 维护回调在安静期不得驱动每分钟 worker 重建，但 heartbeat 必须在恢复边界到期以强制干净重建；重复同类日志应合并，进入积极窗口和恢复成功必须立即记录。该策略只属于 GM live Phoenix 路径，不得进入回测、优化或训练。
