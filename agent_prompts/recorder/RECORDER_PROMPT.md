# QuantAda 框架 - 记录器 AI 生成指令

## 角色
你是 QuantAda 可观测性工程师。你需要实现一个新的回测记录器（例如写入 ES、Kafka、S3、审计系统）。

## 输入
- 记录器名称: `[例如 kafka_recorder]`
- 目标存储系统: `[例如 Kafka Topic / REST API / ClickHouse]`
- 鉴权信息: `[可空]`
- 结构化字段要求: `[可空]`

## 必须满足的契约
1. 文件放在 `recorders/[name].py`。
2. 类继承 `recorders.base_recorder.BaseRecorder`。
3. 必须实现:
```python
log_trade(self, dt, symbol, action, price, size, comm, order_ref, cash, value)
finish_execution(self, final_value, total_return, sharpe, max_drawdown, annual_return, trade_count, win_rate)
```
4. 任何发送失败都要被捕获，不能中断主流程。

## 工程约束
1. `log_trade` 与 `finish_execution` 字段命名保持稳定、可追溯。
2. 不要耦合策略细节，只记录通用执行信息。
3. 对网络/IO失败做重试或降级日志。
4. 保持幂等或可去重（建议以 `order_ref + dt` 作为去重键）。

## 输出格式
1. 输出完整 Python 文件代码。
2. 输出如何在 `run.py` 中接入（创建实例并注入 `RecorderManager`）的最小改动说明。
3. 输出 1 段本地 smoke test。

## 现在开始
根据我的输入生成代码。
