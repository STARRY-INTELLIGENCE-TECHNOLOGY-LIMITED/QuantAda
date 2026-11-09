# QuantAda

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

简体中文 | [English](README_EN.md)

一个优雅、可扩展、可实盘的量化交易框架，实现算法的分模块独立或协作开发。
`Ada` 是 `Adapter`（适配器）的缩写，也借此向计算机先驱 **阿达·洛夫莱斯 (Ada Lovelace)** 及以她命名的 Ada 语言致敬。

本项目旨在对抗市面上普遍存在的“过拟合”与“造神”风气，通过严谨的工程架构与数学逻辑，让量化交易回归敬畏市场、技术为本的初心。
核心思路是通过适配器把策略、数据源、风控和券商执行解耦，保持执行链路清晰、可审计、可恢复。

## 核心特点

| 特点 | 说明 |
| --- | --- |
| **一套策略，三种运行** | 同一份策略代码可用于回测、Optuna 参数优化和实盘/仿真。`run.py` 负责模式分流；回测与优化保持同步、非阻塞，只有实盘才轮询、对账并在有界预算内等待。 |
| **适配器解耦** | 策略、选股器、风控、数据源、券商各自独立扩展。券商支持 GM、IBKR、Futu；数据源责任链覆盖 Tushare、Tiingo、AkShare、EODHD、ThetaData、GM、Futu、IBKR 与 CSV 缓存，另有 SEC、财报、利率、OCC、CBOE 辅助源。 |
| **实盘可恢复** | 账户与订单状态以券商返回为准，不持久化交易意图；断连、拒单、数据失败优先重连、重试、对账和报警。先卖后买，被拒买单同一根 K 线降档重试，A 股 T+1 按可卖数量，隔夜挂单默认自动清理（`KEEP_OVERNIGHT_ORDERS` 可保留）。 |
| **保守的回测口径** | 佣金与滑点同时计入成交和买力预留；支持信号收盘成交（`close`）与次日开盘成交（`next_open`）；行情缓存带数据源与复权口径标记，不会混用。 |
| **期权支持** | 样例覆盖买开、现金担保短 Put、Covered Call、原子 Put Credit Spread；`option_universe` 自动展开期权链；回测到期默认按内在价值现金结算，声明 `option_settlement="physical"` 可做实物交割。 |
| **优化与验证** | Optuna 搜索、训练/测试分窗、多结束日期独立 Study、按完整训练参数生成稳定身份的任务续传。 |
| **开发体验** | Web 命令工作台、本地 MCP 回测 Server，以及 `docs/specs/` 规范与 `agent_prompts/` 模板，便于 AI 辅助二次开发。 |

## 架构

![QuantAda 架构](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/architecture_zh.png?raw=true)

图中重点展示 `run.py` 的模式分流、`Backtester` / `LiveTrader` / `Optimizer` 的职责边界，以及策略扩展、数据责任链、调仓计划与订单执行、券商适配和运行期通知之间的关系。实盘路径包含有界执行预算、柜台状态对账和进程级 heartbeat 自愈；回测与优化保持同步、非阻塞执行。

## 快速开始

### 1) 安装

运行环境为 Python 3.12。

```bash
git clone https://github.com/SUTFutureCoder/QuantAda.git
cd QuantAda

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

券商 SDK 默认保持注释，避免未使用的平台污染环境。需要 GM、IBKR 或 Futu 时，先解除 `requirements.txt` 中对应行的注释，再重新执行 `python -m pip install -r requirements.txt`。

### 2) 配置

`config.py` 保留框架核心配置；数据源凭据在 `configs/providers.py` 中配置，并由统一入口平铺导出。至少配置一个数据源 Token（常用 `TUSHARE_TOKEN`）：

```python
# configs/providers.py
TUSHARE_TOKEN = "your_token_here"
```

可选：开启数据库记录。

```python
DB_ENABLED = True
DB_URL = "sqlite:///quantada_logs.db"
```

配置按责任域拆分，`config.py` 固定列出各 `configs` 子模块并用一行 `import *` 平铺配置；Futu Provider/adapter 使用 `configs/futu.py` 维护的同名键。标准 `run.py` 启动后，同名环境变量会按默认值类型自动转换为配置 fallback，显式 `--config` 覆盖环境变量；测试设置 `QUANTADA_TEST_MODE=1` 后禁止该映射，避免继承真实 Broker、凭据和通知。这里不做目录自动扫描，用户只需理解一个统一入口；`configs/manager.py` 只负责合并 Broker 连接环境并提供报警状态判断：

| 配置文件 | 主要配置项 | 用途 |
| --- | --- | --- |
| `config.py` | `LOT_SIZE`、`DATA_PATH`、`LOG`、`PRINT_PLAN`、`KEEP_OVERNIGHT_ORDERS` | 框架、回测和通用执行 |
| `configs/providers.py` | `TUSHARE_TOKEN`、`SXSC_TUSHARE_TOKEN`、`TIINGO_TOKEN`、`THETADATA_TOKEN`、`EODHD_API_TOKEN`、`SEC_USER_AGENT`、`OCC_ACTIONS_URL/PATH`、`EARNINGS_ARCHIVE_PATH`、`DATA_PROVIDER_COMPOSITIONS`、`DATA_PROVIDER_ALIASES` | 历史行情、估值、调整合约、点时财报与可配置组合 |
| `configs/futu.py` | `FUTU_HOST`、`FUTU_PORT`、`FUTU_RSA_KEY_PATH`、账户/交易键、`FUTU_BROKER_ENVIRONMENTS` | 富途 OpenD 行情与官方交易连接；RSA 路径为空时使用明文协议；常规配置通过 `FUTU_TRADE_PASSWORD_ENV` 或 `FUTU_TRADE_PASSWORD_MD5_ENV` 引用外部环境变量，私有工作台方案可单独保存本机解锁凭据 |
| `configs/alarms.py` | `ALARMS_ENABLED`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET`、`WECOM_WEBHOOK`、`ALARM_LEVEL` | 报警通道 |
| `configs/gm.py` | `GM_TOKEN`、`GM_PRICE_ADJUSTMENT`、`GM_BROKER_ENVIRONMENTS` | GM Broker/连接环境（运行时显示为 `BROKER_ENVIRONMENTS['gm_broker']`）；`GM_PRICE_ADJUSTMENT` 控制历史行情复权口径（1 前复权默认、0 原始价格、2 后复权） |
| `configs/ibkr.py` | `IBKR_HOST`、`IBKR_PORT`、`IBKR_CLIENT_ID`、`IBKR_ORDER_ACCOUNT`、`IB_BROKER_ENVIRONMENTS` | IBKR Broker/连接环境（运行时显示为 `BROKER_ENVIRONMENTS['ib_broker']`） |
| `configs/options.py` | `OPTION_RISK_WATCHDOG_*` | 实盘期权风险 Watchdog 的安全阈值 |

不要把真实 Token、密码或 Webhook 提交到公开仓库；运行时也可使用 `--config` 覆盖合并后的公共键，例如：

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --data_source=tiingo --config "{'PRINT_PLAN': True}"
```

### 3) 命令工作台（Web UI）

本地开发环境推荐先启动命令工作台；桌面系统会自动打开默认浏览器。Linux 无图形界面服务器会自动只启动服务，不强行调用浏览器。

```bash
python run.py --ui
```

直接运行 `python run.py` 会先展示完整参数帮助，再启动命令工作台。服务器可显式指定监听地址和端口：

```bash
python run.py --ui --ui_ip 0.0.0.0 --ui_port 8765 --no-browser
```

Futu 配置页可编辑账户 ID、账户索引和账户现金币种，期权/全球账户无需再手工修改隐藏的 `config.py`。

仓库内置命令目录只包含脱敏示例；本机私有策略索引和连接默认值可放在 Git 忽略的 `.data/command_center/private_catalog.json`，启动工作台时会优先合并该文件。也可用 `QUANTADA_PRIVATE_CATALOG` 指定其它路径。

默认仅监听 `127.0.0.1`。工作台没有认证且能启动子进程，因此默认只接受来自 `localhost` 或本机/内网 IP 的同源请求：跨站请求、以域名作为 Host（防 DNS 重绑定）、公网 IP 访问和非 JSON 的 POST 都会返回 403，请求也不能通过变量指定 `PYTHON_EXECUTABLE` 或 `PYTHON*`/`LD_*`/`DYLD_*`。因此用 `--ui_ip 0.0.0.0` 暴露后，请用内网 IP 访问，不要用主机名。当前会话中的 Token、Webhook、账户配置及 Futu 解锁凭据仍按原样展示，请勿暴露到不受信任网络。仅在调试时可加 `--dangerously-ui-executable` 关闭上述校验，此时任何能访问该端口的人都能在本机执行命令。

Futu / ThetaData / SEC / CBOE / EODHD 的 Provider 组合与研究数据用法见 [数据源组合与研究数据指南](docs/guides/data_sources.md)。

### 4) 基础回测示例

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519
python run.py --help
```

### 5) 常用命令

```bash
# 自动调仓样例（含底仓保护）
python run.py sample_auto_rebalance_strategy --symbols=SHSE.510300,SHSE.510500,SZSE.159915,SHSE.511880 --start_date=20230101

# 使用选股器
python run.py sample_auto_rebalance_strategy --selection=sample_manual_selector --start_date=20240101

# 加载风控模块
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --risk=sample_stop_loss_take_profit,sample_trend_protection

# 覆盖策略参数 / 风控参数
python run.py sample_auto_rebalance_strategy --symbols=SZSE.159915 --params "{'selectTopK': 2, 'roc_period': 10}" --risk_params "{'stop_loss_pct': 0.05}"

# 使用 CSV 缓存 / 强制刷新
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --data_source csv
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --refresh

# 在线数据源拉取后启用本地缓存；同一 data_source 的后续回测/优化自动命中完整缓存
python run.py sample_macd_cross_strategy --symbols=US.AAPL --data_source=theta --config "{'CACHE_DATA': True}"

# 强制绕过缓存重新拉取并合并
python run.py sample_macd_cross_strategy --symbols=US.AAPL --data_source=theta --refresh --config "{'CACHE_DATA': True}"

# 前一日收盘计算信号，下一交易日开盘成交
python run.py sample_auto_rebalance_strategy --symbols=US.SPY,US.QQQ,US.SMH --execution_price=next_open --no_plot
```

行情缓存的每一行带数据源与复权口径标记（`_price_basis`），换数据源或复权口径（例如 `GM_PRICE_ADJUSTMENT`）不会误用旧缓存；没有该标记的旧缓存首次运行会重新拉取。新请求区间与已有缓存不相交时，缓存会被整体替换而不是拼接，因为不同复权基准的价格无法安全拼接。

### 6) 参数优化（Optuna）

```bash
# 进入优化模式
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}"

# 指定训练/测试区间
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}" --train_period 20210101-20221231 --test_period 20230101-20231231 --n_trials 50
```

`--opt_params` 支持扁平空间和策略分组空间。单策略或多策略训练都会按当前策略类的 `params` 自动筛选实际搜索参数，未声明参数不会增加搜索维度：

优化模式的 `--end_date` 支持逗号分隔的多个结束日期，例如 `--end_date 20240930,20250930,20261105`；每个日期会独立训练/测试并使用独立 Study。回测、实盘和 `--fetch_only` 只接受单个结束日期。

```bash
# 扁平空间：每个策略自动取自己声明的参数
python run.py StrategyA,StrategyB --opt_params "{'p1': {'type': 'int', 'low': 1, 'high': 5}, 'p3': {'type': 'float', 'low': 0.1, 'high': 0.9}, 'p5': {'type': 'int', 'low': 1, 'high': 10}}"

# 分组空间：按全限定策略名或类名提供独立空间
python run.py StrategyA,StrategyB --opt_params "{'StrategyA': {'p1': {'type': 'int', 'low': 1, 'high': 5}}, 'StrategyB': {'p5': {'type': 'int', 'low': 1, 'high': 10}}}"
```

从历史任务交互选择并续传，无需重新填写策略和参数：

```bash
python run.py --train_resume
```

相同训练重复执行会安全续传或提示已有任务（身份由完整训练参数生成的稳定 hash 决定），Web 工作台提供相同的分页与日志查看。任务列表、状态判定、日志翻页和跨日恢复规则见 [优化任务续传指南](docs/guides/training_resume.md)。

### 7) 连接实盘/仿真

在 `config.py` 统一入口（Broker 默认连接值位于 `configs/gm.py`、`configs/ibkr.py`、`configs/futu.py`）配置 `BROKER_ENVIRONMENTS`，再通过 `--connect=broker:env` 启动：

```bash
python run.py sample_auto_rebalance_strategy --connect=gm_broker:sim --symbols=SHSE.510300
python run.py sample_auto_rebalance_strategy --connect=gm_broker:real --symbols=SHSE.510300
python run.py sample_auto_rebalance_strategy --connect=ib_broker:sim --symbols=US.AAPL
python run.py sample_auto_rebalance_strategy --connect=futu_broker:sim --data_source=futu --symbols=HK.00700
python run.py sample_auto_rebalance_strategy --connect=futu_broker:real --data_source=futu --symbols=HK.00700
# Futu 行情订阅事件触发（使用 futu_broker:real_event；不与 schedule 同时启用）
python run.py sample_auto_rebalance_strategy --connect=futu_broker:real_event --data_source=futu --symbols=SHSE.600519
```

`execution_price=next_open` 支持日线或 `Day:HH:MM[:SS]` 截止日线股票/ETF 路径；应把 schedule 放在下一交易日开盘附近，并且只使用上一交易日已完成的信号。期权策略请使用 `close`，避免期权报价日、到期结算和开盘成交日错位。

`sample_macd_cross_strategy` 依赖 Backtrader 指标和 `broker.buy()`，只适用于本地回测/优化，不要用于 `--connect`。

期权样例在 `strategies/options/`，覆盖买开 Put/Call、现金担保短 Put、Covered Call 和原子 Put Credit Spread。请使用全限定类名，并用 `--symbols US.MARA` 这类正股代码让 `option_universe` 展开，不要手写静态期权代码。完整开仓/平仓命令写在各样例文件顶部。

```bash
python run.py strategies.options.sample_put_credit_spread_strategy.SamplePutCreditSpreadStrategy --symbols=US.MARA --data_source=futu --connect=futu_broker:real --no_plot
```

### 8) SDK/插件化模式（策略在仓库外）

```bash
# Linux/macOS
export PYTHONPATH=/path/to/QuantAda:/path/to/MyProject

# Windows CMD
set PYTHONPATH=C:\path\to\QuantAda;C:\path\to\MyProject

# 在框架目录执行外部策略
python run.py my_strategies.my_cool_strategy.MyCoolStrategy
python run.py my_strategies.my_cool_strategy --selection=my_selectors.my_selector
```

### 9) MCP Agent 回测接入

仓库提供本地 stdio 的回测 MCP Server：`python -m mcp_backtest_server`。它支持 MCP `2025-11-25` 与旧版协商，`backtest_run` 可用原生 MCP Tasks 返回长任务状态与结果；实盘连接、任意 `--config` 和训练续传/终止入口会被拒绝。默认 CLI 参数和单次环境覆盖采用保守 allowlist。

MCP Server 以启动它的本机用户权限执行子进程，没有认证或多用户隔离，只连接可信的本地 MCP Host，不要作为网络服务暴露。配置、Tasks TTL 和示例见 [`mcp_backtest_server/README.md`](mcp_backtest_server/README.md)，正式契约见 [`docs/specs/contracts/mcp_backtest_server.md`](docs/specs/contracts/mcp_backtest_server.md)。

## 延伸阅读

- [数据源组合与研究数据指南](docs/guides/data_sources.md)：Futu / Theta / SEC / CBOE / EODHD 组合、点时基本面、期权分红规则
- [优化任务续传指南](docs/guides/training_resume.md)：任务列表、状态判定、日志翻页、跨日恢复
- [`docs/specs/`](docs/specs/README.md)：正式规范层，含架构原则、实盘运行语义和各扩展点契约
- [`mcp_backtest_server/README.md`](mcp_backtest_server/README.md)：MCP 回测 Server 配置与示例

## 设计原则

- 无状态优先：账户与订单状态以券商返回为准，避免本地状态漂移。
- 自愈优先：断连、拒单、数据失败优先恢复与降级，不轻易中断。
- 最小改动：优先局部修复，避免状态机膨胀。
- 执行纪律：统一遵循先卖后买、失败告警、可审计日志链路。

## AI与二次开发入口

- `docs/specs/`: 更正式的规范层，适合先理解当前架构、运行语义和扩展契约。
- `agent_prompts/`: 面向 agent / AI 的生成模板层，适合快速生成 broker、strategy、selector、risk、debug fix 等改动输入。
- 推荐顺序：先读 `docs/specs/`，再读 `agent_prompts/`，最后结合当前源码和测试实现。

## 样例截图

### AI辅助Vibe Coding快速实现策略开发

![vibe-coding](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/vibe_coding.png?raw=true)
![vibe-coding](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/vibe_coding_2.png?raw=true)

### 终端执行回测

![backtest_mode_in_terminal](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/backtest_mode_in_terminal.png?raw=true)

### 券商平台执行回测

![backtest_mode_in_broker](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/backtest_mode_in_broker.png?raw=true)

### 券商平台执行实盘

![live_mode_in_broker](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/live_mode_in_broker.png?raw=true)
![live_mode_in_broker_ibkr](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/live_mode_in_broker_ibkr.png?raw=true)

### 框架和自定义策略工程分离

![public_private_split](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/public_private_split.png?raw=true)

### 实时监控并推送实盘操作

![push_live_alarms](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/push_live_alarms.png?raw=true)

### 基于Optuna优化策略参数

![optimizer](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/optimizer.png?raw=true)

### 实时Optuna优化进度看板

![optuna-dashboard](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/optuna-dashboard.png?raw=true)

### 轻量级人机系统监督多臂赌博机

![optimizer-bandit-summary](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/optimizer-bandit-summary.png?raw=true)

## 免责声明

本项目仅用于技术研究与工程实践，不构成投资建议。
任何实盘交易都存在资金损失风险，请在充分回测与模拟验证后再上线。
使用本项目产生的任何损失，需由使用者自行承担。

## 关于作者

- 个人博客: [project256.com](https://project256.com)
- GitHub: [SUTFutureCoder](https://github.com/SUTFutureCoder)

## 许可证

MIT
