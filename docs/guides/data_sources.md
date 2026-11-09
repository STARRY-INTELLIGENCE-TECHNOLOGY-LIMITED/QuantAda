# 数据源组合与研究数据指南

> 本文从 README 迁出，内容未改动。正式契约见 [`docs/specs/contracts/extensions.md`](../specs/contracts/extensions.md)。

Futu 期权 Credit Spread 使用券商原子组合接口；若当前 OpenD 仿真环境返回“不支持组合期权”，框架会安全拒绝，不会拆分成裸腿。需要同时使用历史 IV/IVP 与实时盘口时，可将 `--data_source` 设置为 `theta+futu` 并选择命令工作台的 `theta_futu_global` 配置档案：通用 `OverlayDataProvider` 负责组合流程，Theta/Futu 适配规则由 `HybridDataProvider` 注入；期权历史来自 ThetaData，正股历史用 Futu 未复权价格与期权行权价对齐，实盘当前行由 Futu 快照合并，回测路径不会访问 Futu。声明了 `option_universe` 的期权策略可用正股/ETF 池自动展开历史或当前期权链，不必把到期合约写死在 `--symbols`。私有命令工作台也支持将 Futu 解锁口令保存到 Git 忽略的本机方案文件；内网工作台会按原样显示当前命令与凭据。完整提前指派同步仍需券商提供明确清算事件字段。

Futu 还可向期权 Wheel 提供带公告日的分红事件。实盘 Covered Call 使用当前完整券商事实；历史回测只有版本化 PIT 归档才使用分红前置判断，Futu 当前 corporate-actions 不会被宣称为历史 as-of。策略只使用早于决策日的公告；除息前最后一个交易日若每股分红高于 Call 时间价值，会阻止新开或买平对应短 Call。ThetaData 日线 OHLC 会另取 15:59--16:00 ET 历史 NBBO 并缓存报价量和源时间戳；无有效双边报价时不以 Last 代替，候选不开仓。该分红规则缓解提前指派风险，但不替代券商清算事件，也不构成完整美式期权回测。

Provider 组合在 `configs/providers.py` 的 `DATA_PROVIDER_COMPOSITIONS` 中声明。每项配置指定历史 Provider、实时 Provider 和 `package.module:factory`；新增组合只需实现自己的适配器工厂并注册配置，不需修改 DataManager 或工作台。核心 alias `theta_futu_sec` 只加载行情和 SEC V/W；`theta_futu_research` 才追加点时财报和 Treasury 利率；`us_options_research` 只追加 CBOE。OCC 调整文件和全天 NBBO 都通过显式研究接口调用，避免普通训练放大网络和缓存成本。ETF 或 SEC 不支持的主体应使用 `theta_futu_cboe`，因为显式加入 `sec` 会在无估值快照时阻断价格代理。

CBOE 官方日频指数 CSV 由 `cboe` Provider 按代码读取，例如 `US.VIX`、`US.VIX3M`、`US.VVIX` 和 `US.SKEW`；完整代码、字段语义和 `close-only` 指数限制见 [`docs/specs/contracts/cboe_provider.md`](docs/specs/contracts/cboe_provider.md)。

Futu 历史 K 线配额不可用时，可用 `eodhd_theta_sec` 继续历史 Wheel 研究：EODHD 提供 US 股票 EOD 与声明日分红，ThetaData 提供期权历史链/报价，SEC 提供 filing-date V。先在 `requirements.txt` 解除可选 `eodhd` 行，再安装依赖，并通过环境变量 `EODHD_API_TOKEN` 注入令牌；EODHD 只支持股票/ETF 的日、周、月周期，不提供期权链或实时快照。完整契约见 [`docs/specs/contracts/eodhd_provider.md`](docs/specs/contracts/eodhd_provider.md)。

美国普通股点时基本面估值可选使用 `sec` 辅助源，例如 `--data_source=theta+futu,sec`：SEC Company Facts 提供已披露的 10-K/10-Q 现金流、资本支出和稀释股数，框架按 filing 日期重建 TTM FCFE 并计算 V，策略再按显式 `w_multiple` 计算 W。SEC 原始 JSON 与合并后的行情估值列均有缓存；估值超过 200 天会失效。使用前须在 `SEC_USER_AGENT` 中提供应用标识和真实联系邮箱；ETF、金融公司或基本面字段不足时没有该估值，不会自动用股票价格代理替代。
