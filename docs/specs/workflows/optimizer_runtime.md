# Optimizer Runtime

本文件约束参数优化器运行期的日志与提权行为。

## 1. Terminal Output Log
1. 优化模式启动后，框架必须将当前进程的 stdout/stderr tee 到终端日志文件。
2. 终端必须继续滚动输出；文件写入必须走后台队列，避免磁盘 IO 阻塞训练主路径。
3. 终端日志目录为 `.data/optimizer`。
4. `.data/optuna` 仅用于 Optuna journal/dashboard 数据，不承担终端输出归档职责。
5. 如果后台队列满，允许丢弃日志片段并在日志文件中记录丢弃计数；不允许因此阻塞 trial 计算。

## 2. Elevated Relaunch
1. Windows 自动提权重启必须保留控制台窗口，避免训练结束或异常时窗口直接消失。
2. 自动提权时必须通过 `QUANTADA_OPTIMIZER_TERMINAL_LOG` 环境变量透传同一个日志路径，让提权前后的输出写入同一份日志。
3. 终端日志没有命令行参数入口；用户命令不应包含内部日志路径参数。
4. 终端日志文件名应复用 Optuna study 的日期/周期/metric/市场命名风格，并写入 `.data/optimizer`。
5. Windows 提权命令必须在执行 Python 前显式切回仓库工作目录，不能把 `.data/optuna` 或 `.data/optimizer` 写到 `C:\Windows\System32`。

## 3. Worker Processes
1. 多进程 worker 应追加写入同一份终端日志文件。
2. worker 的日志 tee 同样必须异步，不能引入跨进程锁等待作为训练主路径依赖。

## 4. Validation Reports
1. 优化器最终摘要应同时输出训练后主回测（MainEval）、测试集回测和年度固定窗口回测，方便人工或 AI 直接分析参数稳定性。
2. MainEval 回测窗口必须至少覆盖最近 3 年；如果训练+测试逻辑窗口更长，则覆盖完整训练+测试逻辑窗口。该窗口不包含 warm-up 起点，warm-up 只用于指标计算。
3. MainEval 回测必须优先复用优化器已加载的 `raw_datas` 切片；只有本轮初始请求窗口确实未覆盖目标窗口时，才允许按缺口向数据提供者补拉。
4. 年度固定窗口回测必须复用优化器已加载的 `raw_datas` 切片，不得额外调用数据提供者补拉数据，避免浪费外部行情配额。
5. MainEval 和年度固定窗口都是训练后报告，不参与 trial 目标函数评分，避免显著拖慢机器学习主路径。
