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
6. SDK/网络请求必须使用有限超时；秒级请求的单次超时应明显短于周期。瞬时失败默认总尝试 5 次，作为模块常量而非配置项。外汇/币市等 24x7 数据不得强制使用常规交易时段过滤。
7. Provider-specific 的连接配置放在 `configs/<name>.py`，由 Provider 直接读取；需要用户调整的配置键随 `config.py` 对应责任域的 `import *` 一起平铺，避免额外命名空间和重复 CLI 白名单。入口不使用目录扫描；若 SDK 支持可选加密，空密钥路径应表示关闭加密，不另设重复开关。
8. Provider 使用的第三方 SDK 应采用可选导入；缺少 SDK 时不能阻断其他数据源，并应明确指引用户解除 `requirements.txt` 对应依赖行的注释后重新执行 `python -m pip install -r requirements.txt`。Futu 事件合约期权历史 K 线可在统一接口失败时回退 `request_history_event_contract_kline`，合约乘数只能来自行情元数据，不能写死。
9. Futu 期权链如提供统一查询入口，必须保留 `timestamp`，并输出 `timestamp`、`underlying`、`spot`、`option_symbol`、`option_type`、`strike`、`expiry`、`bid`、`ask`、`last`、`volume`、`open_interest`、`iv`、`delta`、`gamma`、`theta`、`vega`、`rho`、`contract_multiplier`、`currency`。重复、过期、缺少关键字段或合约乘数时必须失败关闭，不能静默补成普通股票或乘数 1。
10. ThetaData Provider 使用可选 `thetadata` SDK，令牌优先从 `THETADATA_API_KEY` 环境变量或 `configs/providers.py` 的 `THETADATA_TOKEN` 读取，也可运行时安全注入；应调用 `stock_history_*` 与 `option_history_*` 历史接口，不得用快照伪造历史；单一合约历史的“今天”按 America/New_York 裁剪；历史展开把窗口右端钳到美东日历前一天，周末仍跳过；显式 as_of=当日失败关闭，不得把昨日链标成当日，不得回退当前快照。OCC 期权代码须解析为根代码、到期日、方向和行权价；日期/epoch 与 `date + ms_of_day` 必须正确转换，SDK 调用必须有界超时，返回字段缺失时安全降级。标准订阅的当前链可回退一阶 Greeks 并合并 OHLC/OI；历史 `as_of` 必须走当日 EOD Greeks/OHLC，不得用当前快照回放。自动发现应过滤 DTE、优先周五到期日，并限制 ATM 附近 strike_range；支持 `expiration='*'` 时，已完成交易日的历史链优先一次批量拉取并在本地按到期日筛选，只有批量接口失败才回退逐到期日请求；未完成当日不得使用 wildcard。同一进程只保持一个 Theta session，历史 as_of 与合约拉取可在该 session 上有界并发复用 gRPC，禁止并发创建第二个客户端。全链请求使用更长有界超时；超时或 `UNAUTHENTICATED/Invalid session ID` 时若仍有其它请求在途则不重认证，仅独占 session 时才关闭自建客户端；多年单合约 EOD 失败时按块重试。No data found 不逐条打印且不重试；超时/瞬时错误有界重试 5 次，失败 as_of 与漏拉合约后置补偿一轮。OptionUniverse 进度行含 empty/fail 计数。Provider 若覆盖 `close()`，优化训练在历史数据阶段结束及报告补拉完成后释放该连接；无连接 Provider 继承基类空实现，批次释放仅调用实际覆盖的 `close()`。
11. `CACHE_DATA=True` 时，DataManager 对单一显式在线 `data_source`（含回测/优化的 `hybrid`）优先复用 `DATA_PATH/market_cache/` 下覆盖完整请求窗口的 CSV；目录不存在时自动创建。期权合约按到期日裁剪该窗口；请求起点早于上市日时，只要 CSV 已接到到期日即视为完整。缺口或 `refresh=True` 才访问在线 Provider 并合并写回。live mode 不得用 CSV 短路当前行情。多 Provider 链和未指定数据源的默认责任链不得因缓存改变顺序。批量取数不逐标的打印命中/写入日志，进度由 `[Auto-Fetch]` 输出。
12. 若提供期权链，实现 `get_option_chain`。历史回放必须支持 `as_of` 并设置 `HISTORICAL_OPTION_CHAIN = True`；当前快照不得冒充历史链。`DataManager.get_option_chain` 不走 CSV 缓存。回测/优化展开可在 `CACHE_DATA=True` 时按 as_of 把候选合约断点写到 `market_cache/option_universe/`，文件名不含取数窗口；窗口平移时在步长内复用最近 as_of，`--refresh` 全量重拉。`theta+futu` 回测正股走 Futu、期权走 Theta；单一期权历史应裁到合约寿命，EOD 请求不得超过 365 天。


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
