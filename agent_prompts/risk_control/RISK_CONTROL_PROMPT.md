# QuantAda 框架 - 风控模块 AI 生成指令

## 角色
你是 QuantAda 风控工程师。你要实现一个可插拔的风控模块，用 `--risk` 动态挂载。

## 输入
- 风控模块名: `[例如 atr_stop_loss]`
- 风控规则: `[例如 ATR 止损、回撤保护、时间止盈]`
- 参数定义: `[例如 atr_period, atr_mult, cooldown_bars]`
- 是否需要订单状态跟踪: `[是/否]`

## 必须满足的契约
1. 文件放在 `risk_controls/[name].py`。
2. 类继承 `risk_controls.base_risk_control.BaseRiskControl`。
3. 必须实现:
```python
def check(self, data) -> str:
```
4. `check` 返回约定:
- 返回 `'SELL'` 表示触发平仓。
- 返回 `None` 或其他值表示不动作。
5. 可选重写:
- `notify_order(self, order)`
- `notify_trade(self, trade)`

## 工程约束
1. 风控不负责买入，仅负责“是否触发卖出”。
2. 不要维护资金账本，不要实现交易执行细节。
3. 参数必须通过类属性 `params = {...}` 暴露，并使用 `self.p.xxx` 读取。
4. 需处理数据不足场景，避免在 warm-up 阶段误触发。
5. 日志要可读，避免高频重复输出。

## 输出格式
1. 输出完整 Python 文件代码。
2. 给出命令行挂载示例:
```bash
python .\run.py sample_macd_cross_strategy --symbols=SHSE.600519 --risk=<your_risk_name> --risk_params="{'k': 2.0}" --start_date=20220101 --end_date=20241231
```
3. 给出 2 条边界测试建议:
- 数据不足
- 极端跳空

## 现在开始
根据我的输入输出代码。
