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
- 升序排序，去重；日线 `Days/1` 按交易日归一化为每个交易日一根 K 线，失败返回 `None`。
- 回测/优化保留历史交易日的完整日线，并在窗口末日保留最新可见行；实盘保留同日最新可验证快照。相同交易日只保留一行，不同标的的日线时间键必须对齐。
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
7. Provider-specific 的连接配置放在 `configs/<name>.py`，由 Provider 直接读取；需要用户调整的配置键随 `config.py` 对应责任域的 `import *` 一起平铺，避免额外命名空间和重复 CLI 白名单。入口不使用目录扫描；若 SDK 支持可选加密，空密钥路径应表示关闭加密，不另设重复开关。核心 alias `theta_futu_sec` 只加载 `thetadata+futu,sec`；高容量辅助源必须通过显式研究接口或 `theta_futu_research` 选择。DataManager 在命令、回测、优化、option chain 与辅助事件查询入口统一展开，保持配置顺序，嵌套循环时报错。命令工作台会显示别名并按展开后的 Provider 显示凭据变量。
8. Provider 使用的第三方 SDK 应采用可选导入；缺少 SDK 时不能阻断其他数据源，并应明确指引用户解除 `requirements.txt` 对应依赖行的注释后重新执行 `python -m pip install -r requirements.txt`。Futu 事件合约期权历史 K 线可在统一接口失败时回退 `request_history_event_contract_kline`，合约乘数只能来自行情元数据，不能写死。
9. Futu 期权链如提供统一查询入口，必须保留 `timestamp`，并输出 `timestamp`、`underlying`、`spot`、`option_symbol`、`option_type`、`strike`、`expiry`、`bid`、`ask`、`last`、`volume`、`open_interest`、`iv`、`delta`、`gamma`、`theta`、`vega`、`rho`、`contract_multiplier`、`currency`。重复、过期、缺少关键字段或合约乘数时必须失败关闭，不能静默补成普通股票或乘数 1。
10. ThetaData Provider 使用可选 `thetadata` SDK，令牌优先从 `THETADATA_API_KEY` 环境变量或 `configs/providers.py` 的 `THETADATA_TOKEN` 读取，也可运行时安全注入；应调用 `stock_history_*` 与 `option_history_*` 历史接口，不得用快照伪造历史；单一合约历史的“今天”按 America/New_York 裁剪；历史展开把窗口右端钳到美东日历前一天，周末仍跳过；显式 as_of=当日失败关闭，不得把昨日链标成当日，不得回退当前快照。OCC 期权代码须解析为根代码、到期日、方向和行权价；日期/epoch 与 `date + ms_of_day` 必须正确转换，SDK 调用必须有界超时，返回字段缺失时安全降级。标准订阅的当前链可回退一阶 Greeks 并合并 OHLC/OI；历史 `as_of` 必须走当日 EOD Greeks/OHLC，不得用当前快照回放。自动发现应过滤 DTE、优先周五到期日，并限制 ATM 附近 strike_range；支持 `expiration='*'` 时，已完成交易日的历史链优先一次批量拉取并在本地按到期日筛选，只有批量接口失败才回退逐到期日请求；未完成当日不得使用 wildcard。同一进程只复用一个 Theta session，历史 as_of 与合约拉取必须在该 session 上串行，禁止并发请求和并发创建第二个客户端。全链请求使用更长有界超时；超时后等待原请求，不另开、不重认证，下一个请求等这条调用结束后再开始；`UNAUTHENTICATED/Invalid session ID` 只在调用已返回且独占 session 时才关闭自建客户端；多年单合约 EOD 失败时按块重试。No data found 不逐条打印且不重试；超时/瞬时错误有界重试 5 次，失败 as_of 与漏拉合约后置补偿一轮。OptionUniverse 进度行含 empty/fail 计数。Provider 若覆盖 `close()`，优化训练在历史数据阶段结束及报告补拉完成后释放该连接；无连接 Provider 继承基类空实现，批次释放仅调用实际覆盖的 `close()`。
11. `CACHE_DATA=True` 时，DataManager 对单一显式在线 `data_source`（含回测/优化的 `hybrid`）优先复用 `DATA_PATH/market_cache/` 下覆盖完整请求窗口的 CSV；包含辅助源的组合自动使用 `DATA_PATH/sources/<展开后的来源组合>/market_cache/` 子目录，用户只配置一个 DATA_PATH 根目录；目录不存在时自动创建。期权合约按到期日裁剪该窗口；请求起点早于上市日时，只要缓存覆盖请求结束日即视为完整。缺口或 `refresh=True` 才访问在线 Provider 并合并写回。live mode 不得用 CSV 短路当前行情。多 Provider 链和未指定数据源的默认责任链不得因缓存改变顺序。批量取数不逐标的打印命中/写入日志，进度由 `[Auto-Fetch]` 输出。回测入口 `run.py` 保持短进度行，只附加 ETA，不在入口累计来源。优化器取数进度行在标的后标明本次是 cache、fetch、fetch-incomplete 或 skip，并带累计 cache/fetch/incomplete/skip 和 ETA，序号上升不代表全量重拉。ETA 与训练共用剩余时间格式化，按最近约 10 分钟完成速度估计，短时缓存或慢请求不把剩余时间打跳；窗口不足时退回已有样本；样本不足 1 秒显示 ETA unavailable，完成时显示 finishing，并行未凑满一轮 worker 时显示 warming up。相对 `DATA_PATH` 固定到 `run.py` 同级，绝对路径才是强指定；不读取工作目录、平级或上级的另一份 `.data`。创建数据目录时尽量使用 `0o777`，新文件 `0o666`。
12. 若提供期权链，实现 `get_option_chain`。历史回放必须支持 `as_of` 并设置 `HISTORICAL_OPTION_CHAIN = True`；当前快照不得冒充历史链。`DataManager.get_option_chain` 不走 CSV 缓存。回测/优化展开可在 `CACHE_DATA=True` 时按 as_of 把候选合约断点写到 `market_cache/option_universe/`，文件名不含取数窗口；窗口平移时在步长内只复用不晚于目标 as_of 的最近历史快照，禁止使用未来链回答更早决策日，`--refresh` 全量重拉。`theta+futu` 的正股历史使用 Futu `NONE` 原始价格以匹配 Theta 期权行权价口径，独立 Futu 源仍可用默认 QFQ；旧 QFQ 缓存必须重取。单一期权历史应裁到合约寿命，EOD 请求不得超过 365 天。
13. 日频指数 CSV Provider 应通过固定白名单按标的代码映射官方文件（例如 `US.VIX`、`US.VIX3M`），使用有限 HTTP 超时、日期过滤、内存按日复用和 DataManager 缓存；代码、文件、OHLC/close-only 字段语义必须写入 Provider 契约文档。指数只作为研究条件数据，不应直接当作可执行下单标的。需要股票走 Futu、期权走 Theta 时使用 `theta+futu,cboe`，不要把指数网页 slug 当成券商 OpenD 代码，也不要用 VIX ETF 价格替代指数而沿用相同阈值。
14. SEC `companyfacts` 可作为 `VALUATION_ONLY=True` 的辅助 Provider：`get_data()` 返回 `None`，另提供 `get_valuation_snapshots(symbol, start_date, end_date)`；DataManager 仅在用户显式把 `sec` 放入 `data_source` 时将估值列合并到主行情帧。SEC 原始 JSON 缓存到 `DATA_PATH/market_cache/sec/`，合并列由 DataManager 行情 CSV 缓存。`SEC_USER_AGENT` 必须包含应用标识与真实联系邮箱；未设置不得访问 SEC。估值可用日期必须取 filing 日期，不能把 report period 当作发布日期。事实字段不足时返回空快照，不得伪造 V/W 或回退价格代理。
15. P0 辅助数据源必须保持与 OHLCV 回退链隔离：`occ` 读取用户配置的 OCC 官方 CSV/JSON 调整归档，缺少可验证调整时返回 `unknown`，不得把调整期权静默当作 100 倍标准合约；`rates` 读取美国财政部日收益率曲线并缓存，研究代码可显式通过 DataManager 注入动态利率，Black-Scholes 缺失时才回退显式默认；`earnings` 只在每日快照带 `snapshot_date/visible_from` 时宣称 point-in-time，当前 Nasdaq 抓取必须标记不可用于历史 as-of。`DataManager.attach_market_inputs()` 不得由普通回测、优化或实盘启动自动调用，策略不能直接导入这些 Provider。
16. P1 研究接口必须显式区分容量：`ThetaDataProvider.get_option_history_quotes()` 使用 regular-session 09:30--16:00 ET 1 分钟接口并按合约缓存全天 NBBO，默认日线窄窗口不能被隐式替换；`get_option_history_trades()` 仅在交易接口和订阅可用时返回成交，不得用报价或 OHLC 冒充。SEC Company Facts 可在同一原始 JSON 缓存上增加 filing-date 收入、利润、SBC、现金、债务、稀释股数和回购字段，缺失字段保留 NaN。
17. 日线期权 EOD OHLC 不是 NBBO。若 Provider 能读取历史报价，应把报价和源时间戳作为独立字段对齐到交易日；不得把 close/last 补成 bid/ask。ThetaData 收盘报价使用 `option_history_quote` 的 1 分钟接口请求 15:59--16:00 ET 窄窗口，同到期日优先 wildcard 并按 31 天拆块。超时和确认无数据必须有不同缓存标记，候选新仓只接受实际未交叉 Bid/Ask。

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

财报事件若由 Provider 支持，统一实现 `get_earnings_calendar(symbols, start_date, end_date, as_of)`；策略只能经 `DataManager` 使用 `data_source`，不能直接调用 SDK。历史 `as_of` 不受支持时必须显式标记，不得把当前日历冒充点时预告。

分红事件若由 Provider 支持，统一实现 `get_dividend_calendar(symbols, start_date, end_date, as_of)`，规范字段为 `symbol`、`ex_date`、`announcement_date`、`cash_per_share` 和 `currency`。回测/优化/实盘通过 DataManager 将事件快照写入行情 DataFrame.attrs；`dividend_calendar_available` 区分已确认空日历与数据缺失，`dividend_calendar_as_of_supported` 只在完整事件均带公告日、有效每股金额和币种时为真。策略按 `announcement_date < decision_date` 过滤，不能把同日或未来公告用于历史决策。Futu 可使用 corporate-actions `dividend_list` 的公告日和除息日，按除息日合并 rehab 每股现金金额，并从公告 statement 补新事件的金额和币种；不允许仅凭 rehab 推断公告日。

ThetaData 分块历史请求首轮结束后，对超时或瞬时失败的日期块最多补偿 5 轮并打印 retry；其它请求仍在途时只追加一轮，剩余缺口标记后留给本轮末尾或下次执行。本轮末尾对失败和不完整标的串行重试最多 5 轮。中途中断或本轮仍失败时，下次执行复用完整缓存，只补缺口并合并。已经返回的部分数据必须保留。OI、Greeks 因超时缺失时不得丢弃 OHLC，也不得把该结果当作完整缓存。`No data found` 不重试、不标记不完整。优化取数在重试后若仍有明显空洞（超过 5%，或任一标的至少 20 个合约缺失超过 10%，或正股无行情），必须拒绝开始训练，不能带着空洞宇宙继续。
