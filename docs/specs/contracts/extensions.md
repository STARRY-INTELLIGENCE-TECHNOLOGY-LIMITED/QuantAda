# 扩展契约

本文件覆盖 selector / risk / data provider / alarm / recorder 的当前扩展约定。

## 1. 选股器
1. 继承 `stock_selectors.base_selector.BaseSelector`
2. 核心方法: `run_selection()`
3. 返回值:
- `list[str]`
- 或以 symbol 为 index 的 `pandas.DataFrame`
4. 不在 selector 内部下单，不调用 broker 发单
5. 可使用 `self.data_manager.get_data(...)`
6. 实盘 `LiveTrader` 只在 init 与数据恢复时调用 `run_selection()`，结果是启动标的池，不是每根 K 重新选股。每日轮动应在策略 `next()` 中从 `self.broker.datas` 挑选子集。
7. 期权合约展开不是重新选股。selector 仍只输出标的/静态代码；声明了 `option_universe` 的策略由运行时在取数前展开历史链，实盘每个 slot 用当前链增补。`get_option_chain` 的历史回放必须带 `as_of`。滚动候选没有 K 线时丢弃该合约并继续；正股/指数或仍有持仓、在途的期权刷新失败仍跳过整轮。


## 2. 风控模块
1. 继承 `risk_controls.base_risk_control.BaseRiskControl`
2. 核心方法: `check(data) -> str`
3. 返回 `'SELL'` 表示触发卖出，其余返回视为不动作
4. 可选实现:
- `notify_order(order)`
- `notify_trade(trade)`
5. 当前引擎支持逗号分隔的多风控链式加载
6. `risk_params` 可为:
- 平铺 dict
- `{risk_name: {...}}` scoped dict

## 3. 数据源
1. 继承 `data_providers.base_provider.BaseDataProvider`
2. 核心方法: `get_data(symbol, start_date, end_date, timeframe, compression)`
3. 必须提供 `PRIORITY`
4. 返回 DataFrame 要求:
- 包含 `open/high/low/close/volume`
- 时间索引为 `DatetimeIndex`
- 升序、去重
- 失败时返回 `None`
5. DataManager 支持单个或多个 `data_source` 名称，多个 provider 可按逗号或空格分隔
6. 日内 provider 必须同时按能力支持 `timeframe='Minutes'|'Seconds'` 与 `compression`；日期参数在日内模式应保留到秒，增量窗口不得擅自扩成整年高频明细。
7. SDK/网络调用必须使用有限超时；秒级请求的单次超时应明显短于数据周期。瞬时失败默认总尝试 5 次，作为模块常量而非配置项。24x7 数据源不得强制应用常规交易时段过滤。
8. `CACHE_DATA=True` 时，DataManager 对单一显式在线 `data_source`（含回测/优化的 `hybrid`/`theta+futu`）优先读取 `DATA_PATH/market_cache/` 下覆盖完整请求窗口的本地 CSV；`DATA_PATH` 或其子目录不存在时自动创建。期权合约按到期日裁剪该窗口，不要求 CSV 覆盖到期后的空白区间；请求起点早于实际上市日时，只要 CSV 已接到到期日即视为完整，不要求覆盖到期日前 364 天。缓存缺口或 `refresh=True` 时才访问在线 Provider，并将新数据合并写回。LiveTrader 已标记 live mode 时必须跳过该完整缓存并重新读取在线事实。多 Provider 链保持原有顺序，默认未指定数据源的责任链不自动改用 CSV。批量取数时 DataManager 不逐标的打印命中/写入/过滤日志，进度由调用方 `[Auto-Fetch]` 输出。
9. Futu Provider 直接读取 `configs/futu.py` 的 `FUTU_HOST`、`FUTU_PORT` 和可选 `FUTU_RSA_KEY_PATH` 连接 OpenD；这些同名公开键由 `config.py` 导入，因此可使用标准 `--config` 覆盖。RSA 路径为空时关闭协议加密。股票、ETF 和普通期权历史统一通过官方 `request_history_kline` 标准化，使用 `max_count` 与 `page_req_key` 分页；普通期权不得回退 `request_history_event_contract_kline`，后者仅适用于预测/事件合约。期权历史只允许 Days、1/5/15/60 Minutes 原生周期。实盘当前时刻的期权快照行可补充同一快照确认的盘口、Greeks、到期日、执行价和乘数；缺失字段仍不得伪造 IVP 或其他风险事实。期权链使用显式的 `get_option_chain` 查询；期权乘数由行情元数据提供给交易 adapter，元数据不可用时不得自行猜测乘数。行情与交易的代码归一化统一使用 `live_trader.adapters.futu_symbols`，不得在两个模块重复维护映射。需要统一模型时使用 `get_option_chain_normalized()`，字段固定为 `timestamp`、`underlying`、`spot`、`option_symbol`、`option_type`、`strike`、`expiry`、`bid`、`ask`、`last`、`volume`、`open_interest`、`iv`、`delta`、`gamma`、`theta`、`vega`、`rho`、`contract_multiplier`、`currency`；重复、过期、缺少关键字段或乘数的链必须失败关闭。
10. Provider-specific SDK 缺失时必须允许其他 Provider 继续加载，并给出解除 `requirements.txt` 对应注释、重新执行 `python -m pip install -r requirements.txt` 的明确指引。
11. ThetaData Provider 使用可选 `thetadata` SDK，令牌优先从环境变量 `THETADATA_API_KEY` 读取，其次读取 `configs/providers.py` 经 `config.py` 平铺的 `THETADATA_TOKEN`，也可由构造函数运行时注入；占位值会安全跳过。ThetaData SDK 当前不返回合约乘数，Provider 只对未调整的标准美股期权使用显式 `standard_market_assumption` 标记的 100 倍乘数；调整后合约不得静默按该值估值。股票与单一期权分别调用历史 EOD/分钟 OHLC 接口；单一合约历史的“今天”按 America/New_York 日历裁剪，未来日期只返回已存在历史并在 attrs 标记。历史展开把窗口右端钳到美东日历前一天，周末仍跳过；显式 as_of=当日失败关闭，不得把昨日链标成当日，不得回退当前快照。标准订阅不提供专业 Greeks 时，期权链可回退一阶 Greeks 与 OHLC/OI 快照，并在 attrs 标记不完整字段；当前快照不得冒充历史链；`as_of` 必须请求当日 `option_history_greeks_eod`/`option_history_eod`，缺少源 timestamp 时必须失败关闭。自动发现历史链按 `min_dte`/`max_dte` 过滤到期日，优先周五到期日，并限制 ATM 附近 `strike_range`，避免把全部行权价和日频到期日打满超时；支持 `expiration='*'` 时，已完成交易日的历史链应优先一次批量拉取并在本地按到期日筛选，只有批量接口失败才回退逐到期日请求；未完成当日不得使用 wildcard。客户端初始化与超时/瞬时错误均有界重试，总尝试次数为 5；若上一构造线程仍未结束，不得并发创建第二个 session。历史 as_of 与合约拉取可在同一 session 上有界并发复用 gRPC；禁止多进程/多客户端重新认证。超时或 `UNAUTHENTICATED/Invalid session ID` 时，若仍有其它请求在途则只标记失效、不重认证；仅当前请求独占 session 时才关闭自建客户端再继续；多年单合约 EOD 失败时按块重试。历史链 No data found 不逐条刷屏且不重试；失败 as_of 扫完后补偿一轮。OptionUniverse 进度行汇总 empty 与 fail。Provider 若覆盖 `close()`，优化器在历史数据阶段结束及报告补拉完成后释放该连接；无连接 Provider 继承基类空实现，批次释放仅调用实际覆盖的 `close()`。
12. 期权/期货等衍生品回测必须在 DataFrame 的 `option_contract_multiplier`、`option_contract_size`、`contract_multiplier` 或 `contract_size` 列，或 `DataFrame.attrs` 中提供正的现金名义乘数；Backtester 会将目标数量、资金、持仓估值和比例手续费统一按该乘数处理，期权专属字段优先于通用默认字段。 窗口外的空期权 feed 必须跳过；窗口内短命合约要对齐正股交易日时钟，不能让最早到期日结束整个回测。
13. 期权生命周期、保证金与 Greeks 工具必须保持纯计算和确定性：支持 OTM 到期归零、ITM Put 指派、ITM Call 行权、现金/实物结算及最小换月；默认现金担保 Put、Covered Call 之外的裸卖和含未定义空头风险的多腿组合失败关闭。显式 Portfolio Margin 模型可对短 Put 使用有限压力参数，但不得把未知风险字段补成安全值；纯多头多腿可用于静态损益/权利金风险分析。Greeks 缺失 IV 时才可回退 HV，非有限值按安全边界处理。动态链刷新必须有界，过期/缺失链不得交易；盘中对冲必须受最大数量、最大换手和盘口状态约束。
14. 通用期权解析、估值、链模型、订单效果、现金义务、Greeks 和生命周期工具统一放在 `common/options/`，不得使用券商前缀或直接导入具体 adapter/Provider/SDK；策略私有目录不得复制同一套通用期权实现。
15. 通用期权到期损益分析统一使用 `common/options/payoff.py`；策略通过 `BaseStrategy.publish_option_payoff()` 推送 Plan。实盘即时推送，回测按计划 key 延迟推送；分析不得直接导入具体 IM 或券商模块。期权成交 `push_trade` 可附带由 `format_payoff_fill_summary()` 生成的短附录，映射逻辑留在实盘引擎侧。
16. 通用期权损益腿必须显式提供正的合约乘数；Greeks 的波动率、利率和股息率默认使用小数形式，百分数输入必须显式声明单位。到期前换月必须使用旧合约实际平仓价，不得伪装成到期指派；股息只有在调用方提供已按除息日筛选的事件时才可入账。
17. 实盘期权风险 Watchdog 只能读取券商事实快照，风险快照不可用或非有限时必须阻断新开仓；可信快照恢复后仅清除 Watchdog 自身来源的阻断，不得永久锁死或清除清算/人工来源；不得由后台线程直接发单。`BaseLiveBroker.submit_option_spread()` 不支持时必须 fail-closed，策略不得先卖裸 Put 再补保护腿。
18. `OptionRiskLeg` 可携带历史波动率、价格/波动率压力元数据；`compute_option_margin(..., portfolio_margin=True)` 是 QuantAda 的自定义有限压力模型，并非券商 Portfolio Margin 复制品。它计算短 Put，并对有可验证保护腿的 Put Spread 按定义风险上限计量，回测仍保持同步、无网络、无等待。
19. 清算/提前指派对账必须以券商快照为事实源；快照不可信时跳过当轮开仓，不得把未知状态解释为空仓。行情和指标缓存不得写入 NaN/Infinity。
20. `BaseLiveBroker.get_option_risk_snapshot()` 仅定义适配器契约，基类不得从历史 DataFrame 或本地缓存推导实盘 Greeks/保证金；没有适配器实现时 Watchdog 不启动。期权清算能力缺失只在账户存在期权风险时阻断新开仓；无期权持仓时不得因缺少事件接口阻断首次开仓。
21. 期权开仓阻断按来源独立维护（Watchdog、清算、人工等）；清除一个来源只能解除该来源，必须保留其它来源的阻断状态。
22. Futu 期权风险快照必须验证期权及底层报价时间戳；缺失或超过 `OPTION_RISK_MAX_QUOTE_AGE_SECONDS` 的报价不得标记为可信。
23. 清算对账返回的 `position_adjustments` 在引擎中优先交给 Broker 的可选 `apply_option_settlement_adjustments()`，策略仍可通过 `reconcile_settlement()` 处理业务归因；不得持久化交易意图。
24. 回测组合订单必须先完成全部腿的静态校验；任一腿提交失败时撤销已创建订单，并对测试/同步 Broker 已产生的成交执行反向回滚，禁止留下裸腿。
25. `data_source=theta+futu`（或 `hybrid`）使用 `HybridDataProvider`：回测/优化的正股/ETF 历史走 Futu，期权合约历史与 `as_of` 链走 ThetaData；实盘当前快照仍走 Futu。历史部分可写入 `CACHE_DATA` CSV，但 live mode 不得用缓存短路当前行情。混合层不得用 ThetaData 历史价格作为实盘成交价。期权当前快照缺失、过期或字段不完整时必须失败关闭；正股快照过期时保留 Futu 历史，不得改走 Theta。
26. Provider 组合由 `config.DATA_PROVIDER_COMPOSITIONS` 显式声明 `historical`、`realtime`、`factory` 和可选 `factory_kwargs`/`factory_options`；`data_providers.overlay_provider.OverlayDataProvider` 只负责无券商/无 Provider 绑定的组合流程，代码映射、字段归一化和新鲜度策略必须由注入的组合适配器负责。组合定义按名称惰性构造，不得改变未指定组合的 Provider 回退链。
27. `DataManager.get_option_chain()` 不走 CSV 缓存。回测/优化的历史展开可把每个成功 as_of 的候选合约写入 `DATA_PATH/market_cache/option_universe/`，供断点续拉；`--refresh` 或 `CACHE_DATA=False` 时不读该断点。实盘展开不写断点。展开只能使用声明 `HISTORICAL_OPTION_CHAIN` 的 Provider（ThetaData 或 Hybrid 的历史侧）；Futu 当前链不得作为历史 as_of。实盘当前链由 Hybrid 在 live_mode 下转给 Futu，缺失时失败关闭，不回退 Theta 末行。 断点文件名不含取数窗口日期；自然日平移后合并旧窗口文件，并在 as_of 步长内复用最近缓存快照。

## 4. 报警通道
1. 继承 `alarms.base_alarm.BaseAlarm`
2. 关键方法:
- `push_text`
- `push_exception`
- `push_trade`
- `push_status`
3. 失败不得抛出未捕获异常，避免影响交易主流程
4. 调仓、执行器、策略基类、broker 基类等核心/基础层不得直接导入具体 IM manager；需要运行期通知时通过 `common.runtime_notifications` 发出通知意图，由 `alarms` 包负责具体通道。
5. `PRINT_PLAN=True` 时，live 运行可即时推送每次计划及策略排名快照；backtest 运行必须只在回测结束时按快照 key 推送最后一条计划/排名，并在报警通道启用时附带本次执行命令、交易归因和最终绩效摘要，本地日志可继续打印每次计划，避免历史区间触发 IM 限流。
6. `ALARMS_ENABLED=None` 为自动模式: 有任一 webhook 时启用报警通道，无 webhook 时不启用；显式 `False` 用于强制禁用。
7. `LOG` 只控制本地详细日志，不作为 IM 推送总开关。
8. 期权成交 `push_trade` 可读取可选 `payoff_summary`：现货参考价、最大盈利/亏损（可无限）和盈亏平衡点。该附录只覆盖同一标的、同一到期日的当前持仓，并在快照滞后时叠加本次成交；组合成交叠加开仓腿且至少两腿才生成附录。混合到期、乘数缺失、已平仓或构建失败时省略附录，且不得影响成交推送。不要附加腿表格或保证金压力，也不为此增加开关。

## 5. 记录器
1. 继承 `recorders.base_recorder.BaseRecorder`
2. 关键方法:
- `log_trade(...)`
- `finish_execution(...)`
3. 单个 recorder 失败不应中断主流程

## 6. 回测绘图范围
1. `plot_scope` 是回测图表展示范围的统一入口。
2. 可用范围包括 `full`、`portfolio`、`portfolio_equity`、`portfolio_drawdown`、`monthly_heatmap`。
3. `full` 不能与其他范围混用；其余范围可用逗号组合，并复用同一次回测结果打开多个窗口。
4. 新增图表范围不得改变策略决策、撮合、优化目标或实盘路径。
