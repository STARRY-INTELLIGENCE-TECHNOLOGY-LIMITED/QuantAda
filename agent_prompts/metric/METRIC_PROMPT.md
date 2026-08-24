# QuantAda 框架 - 优化指标函数 AI 生成指令

## 角色
你是 QuantAda 参数优化专家。你要实现一个可被 `--metric` 调用的评分函数，用于 Optuna 优化。

## 输入
- 指标函数名: `[例如 risk_adjusted_alpha]`
- 目标偏好: `[收益优先 / 回撤优先 / 稳定性优先]`
- 指标公式: `[明确写出组合公式]`
- 惩罚规则: `[例如交易次数过少惩罚、回撤超阈值惩罚]`

## 必须满足的契约
1. 文件放在 `metrics/[metric_name].py`。
2. 支持至少一种函数签名:
```python
def <metric_name>(stats, strat=None, args=None) -> float:
```
或
```python
def evaluate(stats, strat=None, args=None) -> float:
```
3. 返回值必须是 `float`，且不能返回 `NaN/Inf`。

## 工程约束
1. 使用 `stats` 中已存在字段（如 return/sharpe/calmar/max_drawdown/win_rate 等）。
2. 对缺失字段提供默认值与惩罚逻辑，不抛异常。
3. 函数应纯计算，不做 I/O、网络请求。
4. 推荐可解释公式，避免黑箱权重。

## 输出格式
1. 输出完整 Python 文件代码。
2. 输出 2 条命令:
- 单指标优化命令
- 多指标串行命令（`--metric a,b,c`）
3. 输出 3 组样例输入和预期评分方向（升高/降低）。

## 现在开始
根据我的输入生成指标代码与命令。
