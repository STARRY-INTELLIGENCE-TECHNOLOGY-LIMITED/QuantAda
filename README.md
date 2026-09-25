# QuantAda

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

简体中文 | [English](README_EN.md)

一个优雅、可扩展、可实盘的量化交易框架，实现算法的分模块独立或协作开发。
`Ada` 是 `Adapter`（适配器）的缩写，也借此向计算机先驱 **阿达·洛夫莱斯 (Ada Lovelace)** 及以她命名的 Ada 语言致敬。

本项目旨在对抗市面上普遍存在的“过拟合”与“造神”风气，通过严谨的工程架构与数学逻辑，让量化交易回归敬畏市场、技术为本的初心。
核心思路是通过适配器把策略、数据源、风控和券商执行解耦，保持执行链路清晰、可审计、可恢复。

## 快速开始

### 1) 安装

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

配置按责任域拆分，`config.py` 固定列出各 `configs` 子模块并用一行 `import *` 平铺配置；Futu Provider/adapter 使用 `configs/futu.py` 维护的同名键。这里不做目录自动扫描，用户只需理解一个统一入口；`configs/manager.py` 只负责合并 Broker 连接环境并提供报警状态判断：

| 配置文件 | 主要配置项 | 用途 |
| --- | --- | --- |
| `config.py` | `LOT_SIZE`、`DATA_PATH`、`LOG`、`PRINT_PLAN`、`KEEP_OVERNIGHT_ORDERS` | 框架、回测和通用执行 |
| `configs/providers.py` | `TUSHARE_TOKEN`、`SXSC_TUSHARE_TOKEN`、`TIINGO_TOKEN`、`THETADATA_TOKEN`、`DATA_PROVIDER_COMPOSITIONS` | 历史行情 Provider 与可配置组合 |
| `configs/futu.py` | `FUTU_HOST`、`FUTU_PORT`、`FUTU_RSA_KEY_PATH`、账户/交易键、`FUTU_BROKER_ENVIRONMENTS` | 富途 OpenD 行情与官方交易连接；RSA 路径为空时使用明文协议；常规配置通过 `FUTU_TRADE_PASSWORD_ENV` 或 `FUTU_TRADE_PASSWORD_MD5_ENV` 引用外部环境变量，私有工作台方案可单独保存本机解锁凭据 |
| `configs/alarms.py` | `ALARMS_ENABLED`、`DINGTALK_WEBHOOK`、`DINGTALK_SECRET`、`WECOM_WEBHOOK`、`ALARM_LEVEL` | 报警通道 |
| `configs/gm.py` | `GM_TOKEN`、`GM_BROKER_ENVIRONMENTS` | GM Broker/连接环境（运行时显示为 `BROKER_ENVIRONMENTS['gm_broker']`） |
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

默认仅监听 `127.0.0.1`；也可使用 `--ui_ip 0.0.0.0`（或其它明确地址）显式暴露到网络。工作台面向内部使用，当前会话中的 Token、Webhook、账户配置及 Futu 解锁凭据均按原样展示，请勿将监听地址暴露到不受信任网络。

Futu 期权 Credit Spread 使用券商原子组合接口；若当前 OpenD 仿真环境返回“不支持组合期权”，框架会安全拒绝，不会拆分成裸腿。需要同时使用历史 IV/IVP 与实时盘口时，可将 `--data_source` 设置为 `theta+futu` 并选择命令工作台的 `theta_futu_global` 配置档案：通用 `OverlayDataProvider` 负责组合流程，Theta/Futu 适配规则由 `HybridDataProvider` 注入；历史字段来自 ThetaData，实盘当前行由 Futu 快照合并，回测路径不会访问 Futu。声明了 `option_universe` 的期权策略可用正股/ETF 池自动展开历史或当前期权链，不必把到期合约写死在 `--symbols`。私有命令工作台也支持将 Futu 解锁口令保存到 Git 忽略的本机方案文件；内网工作台会按原样显示当前命令与凭据。完整提前指派同步仍需券商提供明确清算事件字段。

Provider 组合在 `configs/providers.py` 的 `DATA_PROVIDER_COMPOSITIONS` 中声明。每项配置指定历史 Provider、实时 Provider 和 `package.module:factory`；新增组合只需实现自己的适配器工厂并注册配置，不需修改 DataManager 或工作台。

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
```

### 6) 参数优化（Optuna）

```bash
# 进入优化模式
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}"

# 指定训练/测试区间
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}" --train_period 20210101-20221231 --test_period 20230101-20231231 --n_trials 50
```

从历史任务交互选择并续传，无需重新填写策略和参数：

```bash
python run.py --train_resume
```

任务按最近更新时间倒序，每页 10 个；`n` / `p` 翻页，`g 3` 跳到第 3 页。输入序号后先查看原始命令，再输入 `y` 确认恢复，`b` 返回列表，`q` 退出。菜单和日志为英文：`1 / y: confirm resume`、`l: view log`。Web 工作台提供相同分页及命令详情，点击 `Resume selected task` 确认。
列表会标明训练状态 `Finished` 或 `Incomplete`。只看与该任务匹配的终端日志末尾展示段：Journal 文件名必须一致；日志若写了窗口或快照，也必须一致。同一 Journal 里有多个任务、而日志没有窗口和快照时，不把该日志判给每一个任务。分析开始标记之后出现结束标记，且结束标记后不是空摘要，才算 `Finished`；空摘要会跳过。否则为 `Incomplete`。`Incomplete` 仍可续传。
选中任务后可输入 `l` 查看该任务匹配的最新终端日志，不会打开另一个任务的日志，默认停在末尾。`n` / `p` 翻页，`h` / `e` / `m` 跳到首页、末尾和中间，`s` 跳到分析展示段，`g 3` 跳到第 3 页，`b` 返回详情。Web 工作台的 `View log` 提供相同翻页。Trial 进度在同一行显示 `Trial 369/2160 ETA 6h12m`。
跨日恢复优先使用固定的数据、选股结果和期权池快照。旧任务没有快照时仍加载原 Study 并复用已完成试验；同一批次已有快照会实际加载并跳过取数，旧评分计入预算但不算该快照的结果。本任务快照损坏时重新准备数据并绑定新快照，不改用其它 Study 的快照，也不另开空 Study。详情页同时提供可复制的“使用最新行情手动重新训练”命令：移除旧 Study 绑定并追加 `--refresh`，重新选股、取数和训练；未显式指定的日期按启动时推断，显式日期可手动调整。
未记录当前 worker 配置口径的旧任务会按原 Study 名称加载，已完成试验计入预算，只跑未完成组合。控制台 Trial 编号与 Journal trial_id 一致，从原 Study 继续，不是从 0 开始。同一批次里完成更多的 Study 优先，避免薄的隔离 Study 丢掉已探索参数。列表和预览会写明这一口径。

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

## 核心设计（简版）

- 无状态优先：账户与订单状态以券商返回为准，避免本地状态漂移。
- 自愈优先：断连、拒单、数据失败优先恢复与降级，不轻易中断。
- 最小改动：优先局部修复，避免状态机膨胀。
- 执行纪律：统一遵循先卖后买、失败告警、可审计日志链路。

![QuantAda 架构](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/architecture_zh.png?raw=true)

图中重点展示 `run.py` 的模式分流、`Backtester` / `LiveTrader` / `Optimizer` 的职责边界，以及策略扩展、数据责任链、调仓计划与订单执行、券商适配和运行期通知之间的关系。实盘路径包含有界执行预算、柜台状态对账和进程级 heartbeat 自愈；回测与优化保持同步、非阻塞执行。

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
