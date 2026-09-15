# QuantAda

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[简体中文](README.md) | English

An elegant, extensible, live-trading-ready quantitative trading framework for developing algorithm modules independently or collaboratively. `Ada` is short for `Adapter`, and also pays tribute to computing pioneer **Ada Lovelace** and the Ada programming language named after her.

QuantAda pushes back against the overfitting and guru culture common in quantitative trading. It brings the focus back to disciplined engineering, sound mathematical reasoning, and respect for markets. Its central idea is to decouple strategies, data providers, risk controls, and broker execution through adapters, keeping execution paths clear, auditable, and recoverable.

## Quick Start

### 1. Install

```bash
git clone https://github.com/SUTFutureCoder/QuantAda.git
cd QuantAda

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Broker SDKs are commented out by default to keep the base environment small. Before using GM, IBKR, or Futu, uncomment the corresponding line in `requirements.txt` and rerun `python -m pip install -r requirements.txt`.

### 2. Configure

`config.py` keeps the framework core settings. Configure data-provider credentials in `configs/providers.py`; the single configuration entry point re-exports them. Set at least one data-provider token (commonly `TUSHARE_TOKEN`):

```python
# configs/providers.py
TUSHARE_TOKEN = "your_token_here"
```

Optionally enable database recording:

```python
DB_ENABLED = True
DB_URL = "sqlite:///quantada_logs.db"
```

Configuration is split by responsibility. `config.py` lists the `configs` submodules explicitly and flattens each with one `import *` line; the Futu Provider and adapter use the same-named keys owned by `configs/futu.py`. There is no directory auto-discovery, so users only need one configuration facade. `configs/manager.py` only combines broker connection environments and provides alarm-state helpers:

| Configuration file | Main settings | Purpose |
| --- | --- | --- |
| `config.py` | `LOT_SIZE`, `DATA_PATH`, `LOG`, `PRINT_PLAN`, `KEEP_OVERNIGHT_ORDERS` | Framework, backtest, and common execution |
| `configs/providers.py` | `TUSHARE_TOKEN`, `SXSC_TUSHARE_TOKEN`, `TIINGO_TOKEN`, `THETADATA_TOKEN`, `DATA_PROVIDER_COMPOSITIONS` | Historical providers and configurable compositions |
| `configs/futu.py` | `FUTU_HOST`, `FUTU_PORT`, `FUTU_RSA_KEY_PATH`, account/trading keys, `FUTU_BROKER_ENVIRONMENTS` | Futu OpenD quote and official trading connection; an empty RSA path means plaintext protocol; normal config references unlock credentials through `FUTU_TRADE_PASSWORD_ENV` or `FUTU_TRADE_PASSWORD_MD5_ENV`, while private Command Center profiles may store a local credential |
| `configs/alarms.py` | `ALARMS_ENABLED`, `DINGTALK_WEBHOOK`, `DINGTALK_SECRET`, `WECOM_WEBHOOK`, `ALARM_LEVEL` | Alarm channels |
| `configs/gm.py` | `GM_TOKEN`, `GM_BROKER_ENVIRONMENTS` | GM broker/connection environments (exposed at runtime as `BROKER_ENVIRONMENTS['gm_broker']`) |
| `configs/ibkr.py` | `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_ORDER_ACCOUNT`, `IB_BROKER_ENVIRONMENTS` | IBKR broker/connection environments (exposed at runtime as `BROKER_ENVIRONMENTS['ib_broker']`) |
| `configs/options.py` | `OPTION_RISK_WATCHDOG_*` | Live option risk watchdog safety thresholds |

Do not commit real tokens, passwords, or webhooks to a public repository. You can also override a merged public key at runtime, for example:

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --data_source=tiingo --config "{'PRINT_PLAN': True}"
```

### 3. Command Center (Web UI)

```bash
python run.py --ui
python run.py --ui --ui_ip 0.0.0.0 --ui_port 8765 --no-browser
```

The Futu section also exposes account ID, account index, and account cash currency, so global/option accounts do not require hidden `config.py` edits.

The repository catalog contains sanitized examples only. Put private strategy indexes and connection defaults in the Git-ignored `.data/command_center/private_catalog.json`; the Command Center merges it with priority at startup. `QUANTADA_PRIVATE_CATALOG` can point to another private JSON file.

The default bind address is `127.0.0.1`. The UI is intended for trusted internal use and displays current session variables, including Futu unlock credentials; only specify `--ui_ip` when you explicitly want to expose another interface.

Futu option credit spreads use the broker's atomic combo-order API. If the current OpenD simulation environment rejects combo options, QuantAda fails closed and does not split the order into naked legs. Use `--data_source=theta+futu` with the Command Center `theta_futu_global` profile when historical IV/IVP should come from ThetaData and the current live row from Futu; the generic `OverlayDataProvider` owns composition while `HybridDataProvider` supplies Theta/Futu-specific mapping. Option strategies that set `option_universe` can expand an underlying pool into historical or live option contracts instead of hard-coding expiries in `--symbols`. Backtests do not call Futu. The private Command Center may also store a Futu unlock password in the Git-ignored local profile store; trusted internal command previews show current values as-is. Explicit assignment reconciliation still requires broker clearing-event fields.

Provider compositions are declared in `configs/providers.py` under `DATA_PROVIDER_COMPOSITIONS`. Each entry names a historical Provider, a realtime Provider, and a `package.module:factory`; adding another pair only requires a new adapter factory and configuration, not DataManager or Command Center changes.

### 4. Basic Backtest

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519
python run.py --help
```

### 5. Common Commands

```bash
# Auto-rebalancing example with reserve-position protection
python run.py sample_auto_rebalance_strategy --symbols=SHSE.510300,SHSE.510500,SZSE.159915,SHSE.511880 --start_date=20230101

# Use a stock selector
python run.py sample_auto_rebalance_strategy --selection=sample_manual_selector --start_date=20240101

# Load multiple risk-control modules
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --risk=sample_stop_loss_take_profit,sample_trend_protection

# Override strategy and risk-control parameters
python run.py sample_auto_rebalance_strategy --symbols=SZSE.159915 --params "{'selectTopK': 2, 'roc_period': 10}" --risk_params "{'stop_loss_pct': 0.05}"

# Use the CSV cache or force a refresh
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --data_source csv
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --refresh

# Cache an explicit online source locally for repeated backtests/optimization
python run.py sample_macd_cross_strategy --symbols=US.AAPL --data_source=theta --config "{'CACHE_DATA': True}"

# Force an online refresh and merge it into the cache
python run.py sample_macd_cross_strategy --symbols=US.AAPL --data_source=theta --refresh --config "{'CACHE_DATA': True}"
```

### 6. Parameter Optimization (Optuna)

```bash
# Enter optimization mode
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}"

# Set explicit training and test periods
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}" --train_period 20210101-20221231 --test_period 20230101-20231231 --n_trials 50
```

### 7. Connect to Live Trading or Simulation

Configure `BROKER_ENVIRONMENTS` through the `config.py` entry point (broker defaults live in `configs/gm.py`, `configs/ibkr.py`, and `configs/futu.py`), then launch with `--connect=broker:env`:

```bash
python run.py sample_auto_rebalance_strategy --connect=gm_broker:sim --symbols=SHSE.510300
python run.py sample_auto_rebalance_strategy --connect=gm_broker:real --symbols=SHSE.510300
python run.py sample_auto_rebalance_strategy --connect=ib_broker:sim --symbols=US.AAPL
python run.py sample_auto_rebalance_strategy --connect=futu_broker:sim --data_source=futu --symbols=HK.00700
python run.py sample_auto_rebalance_strategy --connect=futu_broker:real --data_source=futu --symbols=HK.00700
# Futu quote-subscription event trigger (use futu_broker:real_event; do not combine with schedule)
python run.py sample_auto_rebalance_strategy --connect=futu_broker:real_event --data_source=futu --symbols=SHSE.600519
```

`sample_macd_cross_strategy` depends on Backtrader indicators and `broker.buy()`, so it is backtest/optimizer only. Do not launch it with `--connect`.

Option samples live in `strategies/options/` and cover long put/call, cash-secured short put, covered call, and atomic put credit spreads. Use the fully qualified class name and pass the underlying in `--symbols` so `option_universe` can expand contracts. Copy the full open/close commands from the top of each sample file.

```bash
python run.py strategies.options.sample_put_credit_spread_strategy.SamplePutCreditSpreadStrategy --symbols=US.MARA --data_source=futu --connect=futu_broker:real --no_plot
```

### 8. SDK / Plugin Mode (Strategies Outside This Repository)

```bash
# Linux/macOS
export PYTHONPATH=/path/to/QuantAda:/path/to/MyProject

# Windows CMD
set PYTHONPATH=C:\path\to\QuantAda;C:\path\to\MyProject

# Run an external strategy from the framework directory
python run.py my_strategies.my_cool_strategy.MyCoolStrategy
python run.py my_strategies.my_cool_strategy --selection=my_selectors.my_selector
```

## Core Design

- Stateless first: broker-reported account and order state is the source of truth, preventing local state drift.
- Self-healing first: connection loss, rejected orders, and data failures trigger recovery and degradation paths before termination.
- Minimal change first: prefer targeted fixes and avoid unnecessary state-machine growth.
- Execution discipline: consistently sell before buying, alert on failures, and preserve an auditable log trail.

![QuantAda Architecture](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/architecture_en.png?raw=true)

The diagram focuses on mode routing through `run.py`, the responsibility boundaries among `Backtester`, `LiveTrader`, and `Optimizer`, and the relationships between extension contracts, the data-provider chain, rebalancing and order execution, broker adapters, and runtime services. The live path includes a bounded execution budget, broker-state reconciliation, and process-level heartbeat recovery; backtests and optimization remain synchronous and non-blocking.

## AI and Extension Development

- `docs/specs/`: the formal specification layer for understanding current architecture, runtime semantics, and extension contracts.
- `agent_prompts/`: generation templates for agent-assisted broker, strategy, selector, risk-control, and debugging changes.
- Recommended order: read `docs/specs/`, then `agent_prompts/`, and finally validate against current source code and tests.

## Screenshots

### AI-Assisted Strategy Development

![vibe-coding](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/vibe_coding.png?raw=true)
![vibe-coding](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/vibe_coding_2.png?raw=true)

### Backtesting in the Terminal

![backtest_mode_in_terminal](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/backtest_mode_in_terminal.png?raw=true)

### Backtesting on a Broker Platform

![backtest_mode_in_broker](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/backtest_mode_in_broker.png?raw=true)

### Live Trading on Broker Platforms

![live_mode_in_broker](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/live_mode_in_broker.png?raw=true)
![live_mode_in_broker_ibkr](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/live_mode_in_broker_ibkr.png?raw=true)

### Separating the Framework and Custom Strategy Projects

![public_private_split](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/public_private_split.png?raw=true)

### Monitoring and Pushing Live Trading Events

![push_live_alarms](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/push_live_alarms.png?raw=true)

### Optuna-Based Strategy Optimization

![optimizer](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/optimizer.png?raw=true)

### Live Optuna Progress Dashboard

![optuna-dashboard](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/optuna-dashboard.png?raw=true)

### Lightweight Human-Supervised Multi-Armed Bandit

![optimizer-bandit-summary](https://github.com/SUTFutureCoder/QuantAda/blob/main/.sample_pictures/optimizer-bandit-summary.png?raw=true)

## Disclaimer

This project is intended solely for technical research and engineering practice. It does not constitute investment advice. Live trading involves a risk of financial loss. Perform thorough backtesting and simulation before deployment. You are solely responsible for any losses resulting from use of this project.

## Author

- Blog: [project256.com](https://project256.com)
- GitHub: [SUTFutureCoder](https://github.com/SUTFutureCoder)

## License

MIT
