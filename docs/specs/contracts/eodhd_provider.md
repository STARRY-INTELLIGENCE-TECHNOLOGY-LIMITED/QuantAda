# EODHD Provider 契约

`data_providers.eodhd_provider.EODHDDataProvider` 是可显式选择的 US 股票历史 EOD 备用源。

## 数据能力

- 输入接受 `US.TICKER` 或裸 US ticker；发送给 EODHD 的代码为 `TICKER.US`。多段 share class 代码中的点转换为连字符，例如 `US.BRK.B` 映射为 `BRK-B.US`。
- 只支持 `Days/1`、`Weeks/1`、`Months/1`。分钟、秒和大于 1 的本地压缩周期返回 `None`，不伪装成支持。
- 结果只保留 `open/high/low/close/volume`，使用 EODHD 原始 `close`，不使用 `adjusted_close`，以保持与期权行权价一致的价格口径。
- 日期索引是无时区 `DatetimeIndex`，按交易日归一化、升序、去重，并裁剪到请求窗口。
- 不提供期权行情、历史链、当前快照或订单数据。期权合约由其它显式 Provider 提供，例如 `eodhd,theta,sec` 中的 ThetaData。
- 可通过 EODHD 历史分红接口提供 `ex_date`、`declarationDate`、每股现金额和币种；只有完整事件同时带声明日、正每股金额和币种时，才标记历史 as-of 支持。事件供 DataManager 附加到行情 `attrs`，策略仍按声明日早于决策日过滤。

## 认证与网络

- 令牌优先读取环境变量 `EODHD_API_TOKEN`，其次读取 `config.EODHD_API_TOKEN`。
- `requirements.txt` 中的 `eodhd` SDK 按可选依赖注释。使用前解除该行注释，再执行 `python -m pip install -r requirements.txt`。SDK 缺失时不能阻止其它 Provider 加载。
- Provider 覆盖 SDK 的 HTTP 请求函数，连接/读取超时分别为 5/20 秒，瞬时错误最多尝试 5 次。HTTP 401/403、HTTP 200 的 warning/error 对象（含单元素 JSON 数组），以及 KeyError/TypeError/ValueError 解析失败都不重试，空数据返回 `None`。
- SDK 使用 `requests`，自动遵循 `HTTP_PROXY` 和 `HTTPS_PROXY`。日志不得输出完整请求 URL 或 API token。

## 验证命令

```powershell
$env:EODHD_API_TOKEN = '<从密钥管理器注入>'
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
python run.py sample_macd_cross_strategy --symbols=US.AAPL --data_source=eodhd --start_date=20240101 --end_date=20241231 --no_plot
```
