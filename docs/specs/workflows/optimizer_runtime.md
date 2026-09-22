# 优化器运行期

本文件约束参数优化器运行期的日志与提权行为。

## 1. 终端输出日志
1. 优化模式启动后，框架必须将当前进程的 stdout/stderr tee 到终端日志文件。
2. 终端必须继续滚动输出；文件写入必须走后台队列，避免磁盘 IO 阻塞训练主路径。
3. 终端日志目录为 `.data/optimizer`。
4. `.data/optuna` 仅用于 Optuna journal/dashboard 数据，不承担终端输出归档职责。
5. 如果后台队列满，允许丢弃日志片段并在日志文件中记录丢弃计数；不允许因此阻塞 trial 计算。

## 2. 提权重启
1. Windows 自动提权重启必须保留控制台窗口，避免训练结束或异常时窗口直接消失。
2. 自动提权时必须通过 `QUANTADA_OPTIMIZER_TERMINAL_LOG` 环境变量透传同一个日志路径，让提权前后的输出写入同一份日志。
3. 终端日志没有命令行参数入口；用户命令不应包含内部日志路径参数。
4. 终端日志文件名应复用 Optuna study 的日期/周期/metric/市场命名风格，并写入 `.data/optimizer`。
5. Windows 提权命令必须在执行 Python 前显式切回仓库工作目录，不能把 `.data/optuna` 或 `.data/optimizer` 写到 `C:\Windows\System32`。
6. Windows 提权重启必须把当前进程环境变量写入新的管理员 PowerShell；`ShellExecute runas` 不会继承当前会话的 `$env:`。转发后必须再覆盖 `QUANTADA_DISABLE_AUTO_ELEVATE=1`，避免递归提权。Unix 继续使用 `sudo -E`。

## 3. 工作进程
1. 多进程 worker 应追加写入同一份终端日志文件。
2. worker 的日志 tee 同样必须异步，不能引入跨进程锁等待作为训练主路径依赖。
3. worker 遇到内存压力时应停止本轮 worker 并保留已写入 JournalStorage 的 completed trials；父进程可用已完成结果继续生成报告，不应把已有训练成果丢弃。
4. worker 因内存压力导致进程池破裂或异常退出时，父进程应停止当前 metric 的剩余 worker，并继续使用 JournalStorage 中已完成的 trials；不应把该 metric 直接判为致命崩溃。
5. TPE `n_ei_candidates` 应保守动态调整：普通训练与共享内存可用的多进程训练保持 Optuna 默认候选数；仅在长跑 trial 规模且 spawn 数据共享不可用、需要回退 payload copy 时，按 trial 规模做对数降档以降低内存峰值。

## 4. 验证报告
1. 优化器最终摘要应同时输出训练后主回测（MainEval）、测试集回测和年度固定窗口回测，方便人工或 AI 直接分析参数稳定性。
2. MainEval 回测窗口必须至少覆盖最近 3 年；如果训练+测试逻辑窗口更长，则覆盖完整训练+测试逻辑窗口。该窗口不包含 warm-up 起点，warm-up 只用于指标计算。
3. MainEval 回测必须优先复用优化器已加载的 `raw_datas` 切片；切片前须把 tz-aware 索引转到 naive UTC，避免与窗口边界比较失败后误去补拉。只有本轮初始请求窗口确实未覆盖目标窗口时，才允许按缺口向数据提供者补拉。窗口对齐周期性打印 reuse/fetch 进度；期权末根早于窗口结束时不逐合约刷屏。
4. 年度固定窗口回测必须复用优化器已加载的 `raw_datas` 切片，不得额外调用数据提供者补拉数据，避免浪费外部行情配额。
5. MainEval 和年度固定窗口都是训练后报告，不参与 trial 目标函数评分，避免显著拖慢机器学习主路径。
6. 期权策略评分可使用期权专用 metric；优化器应向评分函数提供 Short Put/PCS 的风险资本、权利金、MAE、最差交易和尾部损失汇总，不得只用账户级收益率替代担保资金收益率。
7. 有限离散参数空间必须使用去重网格采样；`n_trials` 不得通过并行 TPE 重复执行相同参数组合。连续参数空间仍可使用 TPE。

## 5. 启动调度
1. `--opt_schedule` 只负责优化/训练进程进入优化器前的启动等待，不改变 trial、worker 或回测的同步执行语义。
2. 支持 `HH:MM[:SS]` 单次本机时间触发，以及 `Nd|Nw|Nm|Nh[:HH:MM[:SS]]` 周期触发；`N` 为正整数，`Nw` 以本机周一为锚点。
3. `HH:MM[:SS]` 等待当天或次日该时刻，然后才自动推断 start/end；周期规则等待下一次槽位。等待使用阻塞睡眠，不要忙等占用 CPU。实盘 schedule 不接受一次性 HH:MM。
