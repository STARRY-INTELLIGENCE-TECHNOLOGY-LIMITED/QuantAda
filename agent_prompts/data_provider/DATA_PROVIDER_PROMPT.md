# QuantAda 框架 - 数据源适配器 AI 生成指令

## 角色
你是 QuantAda 数据层架构工程师。你需要为框架生成一个可直接接入的数据源适配器。

## 输入
- 提供者名称: `[例如 AlphaVantage / Polygon / 自建REST]`
- 数据接口文档: `[粘贴 API 文档或字段样例]`
- 支持市场与代码格式: `[例如 A股 SHSE.510300 / 美股 AAPL]`
- 认证方式: `[token/key/signature]`
- 速率限制: `[每分钟/每秒请求限制]`

## 必须满足的契约
1. 新建文件到 `data_providers/[name]_provider.py`。
2. 主类命名为 `[Name]DataProvider`，继承 `data_providers.base_provider.BaseDataProvider`。
3. 必须实现:
```python
def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
             timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
```
4. 返回的 `DataFrame` 必须:
- 含 `open/high/low/close/volume` 字段。
- 以时间索引为 `DatetimeIndex`，索引名建议为 `datetime`。
- 升序排序，去重，失败返回 `None`。
5. 必须提供 `PRIORITY`（数值越小优先级越高）。
6. 必须容错:
- 网络失败、空返回、字段缺失要有降级与日志。
- 不允许因为单标的失败导致整个流程崩溃。

## 工程约束
1. 不要改动 `BaseDataProvider` 签名。
2. 尽量不要在 provider 内写业务策略逻辑，只做“数据获取+标准化”。
3. 如需缓存，遵循 `DataManager` 的缓存流程，不在此处重复造轮子。
4. 日期入参兼容 `YYYYMMDD` 与标准时间字符串。
5. 日内场景要正确处理 `timeframe='Minutes'|'Seconds'` 和 `compression`；时间参数保留到秒，避免把秒/分钟级增量请求扩成整年明细。
6. SDK/网络请求必须使用有限超时；秒级请求的单次超时应明显短于周期。外汇/币市等 24x7 数据不得强制使用常规交易时段过滤。
7. Provider-specific 的连接配置放在 `configs/<name>.py`，由 Provider 直接读取；需要用户调整的配置键随 `config.py` 对应责任域的 `import *` 一起平铺，避免额外命名空间和重复 CLI 白名单。入口不使用目录扫描；若 SDK 支持可选加密，空密钥路径应表示关闭加密，不另设重复开关。
8. Provider 使用的第三方 SDK 应采用可选导入；缺少 SDK 时不能阻断其他数据源，并应明确指引用户解除 `requirements.txt` 对应依赖行的注释后重新执行 `python -m pip install -r requirements.txt`。Futu 事件合约期权历史 K 线可在统一接口失败时回退 `request_history_event_contract_kline`，合约乘数只能来自行情元数据，不能写死。

## 输出格式
1. 输出完整 Python 文件代码。
2. 在代码后给出最小验证命令:
```bash
python .\run.py sample_macd_cross_strategy --symbols=<example_symbol> --data_source=<provider_alias> --start_date=20240101 --end_date=20241231 --no_plot
```
3. 给出 3 条自检清单:
- 字段完整性
- 时区和索引
- 空数据兜底

## 现在开始
基于我的输入生成完整代码与验证命令。
