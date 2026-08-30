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

配置按责任域拆分，但运行时仍由 `config.py` 平铺导出，现有代码和 `--config` 用法无需区分配置来源。`configs/manager.py` 只负责合并 Broker 连接环境并提供报警状态判断：

| 配置文件 | 主要配置项 | 用途 |
| --- | --- | --- |
| `config.py` | `LOT_SIZE`、`DATA_PATH`、`LOG`、`PRINT_PLAN`、`KEEP_OVERNIGHT_ORDERS` | 框架、回测和通用执行 |
| `configs/providers.py` | `TUSHARE_TOKEN`、`SXSC_TUSHARE_TOKEN`、`TIINGO_TOKEN` | 历史行情 Provider |
| `configs/alarms.py` | `ALARMS_ENABLED`、`DINGTALK_WEBHOOK`、`WECOM_WEBHOOK`、`ALARM_LEVEL` | 报警通道 |
| `configs/gm.py` | `GM_TOKEN`、`GM_BROKER_ENVIRONMENTS` | GM Broker/连接环境（运行时显示为 `BROKER_ENVIRONMENTS['gm_broker']`） |
| `configs/ibkr.py` | `IBKR_HOST`、`IBKR_PORT`、`IBKR_CLIENT_ID`、`IBKR_ORDER_ACCOUNT`、`IB_BROKER_ENVIRONMENTS` | IBKR Broker/连接环境（运行时显示为 `BROKER_ENVIRONMENTS['ib_broker']`） |

不要把真实 Token、密码或 Webhook 提交到公开仓库；运行时也可使用 `--config` 覆盖合并后的公共键，例如：

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --data_source=tiingo --config "{'PRINT_PLAN': True}"
```

### 3) 基础回测示例

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519
python run.py --help
```

### 4) 常用命令

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
```

### 5) 参数优化（Optuna）

```bash
# 进入优化模式
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}"

# 指定训练/测试区间
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}" --train_period 20210101-20221231 --test_period 20230101-20231231 --n_trials 50
```

### 6) 连接实盘/仿真

在 `config.py` 统一入口（Broker 默认连接值位于 `configs/gm.py`、`configs/ibkr.py`）配置 `BROKER_ENVIRONMENTS`，再通过 `--connect=broker:env` 启动：

```bash
python run.py sample_macd_cross_strategy --connect=gm_broker:sim
python run.py sample_macd_cross_strategy --connect=gm_broker:real
python run.py sample_macd_cross_strategy --connect=ib_broker:sim
```

### 7) SDK/插件化模式（策略在仓库外）

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
