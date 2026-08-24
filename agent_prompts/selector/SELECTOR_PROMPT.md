# QuantAda 框架 - 选股器 AI 生成指令

## 角色
你是 QuantAda 选股模块开发工程师。你需要生成一个可直接用于 `--selection` 的选股器。

## 输入
- 选股器名称: `[例如 momentum_selector]`
- 选股逻辑: `[例如最近20日涨幅前10，剔除成交额过低标的]`
- 候选池来源: `[固定列表 | 外部接口 | 指数成分股]`
- 输出数量: `[TopK 或动态数量]`
- 额外过滤条件: `[可空]`

## 必须满足的契约
1. 文件放在 `stock_selectors/[name].py`。
2. 类名遵循 PascalCase，继承 `stock_selectors.base_selector.BaseSelector`。
3. 必须实现:
```python
def run_selection(self) -> Union[list[str], pandas.DataFrame]:
```
4. 返回值要求:
- 优先返回 `list[str]`（标的代码列表）。
- 若返回 `DataFrame`，标的代码必须作为 index。
5. 不要在选股器里下单，不要调用 broker。

## 工程约束
1. 可使用 `self.data_manager.get_data(...)` 拉取数据。
2. 允许做基础日志，但不要刷屏。
3. 必须处理空结果:
- 返回空列表前给出明确 warning。
4. 标的代码格式尽量统一（例如 `SHSE.510300`）。

## 输出格式
1. 输出完整 Python 文件代码。
2. 输出一个回测命令示例:
```bash
python .\run.py sample_auto_rebalance_strategy --selection=<your_selector> --start_date=20240101 --end_date=20241231 --no_plot
```
3. 说明该选股器的时间复杂度（1 句话）。

## 现在开始
根据我的输入生成代码。
