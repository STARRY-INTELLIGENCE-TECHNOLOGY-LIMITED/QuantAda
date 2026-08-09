# QuantAda

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[简体中文](README.md) | English

An elegant, extensible, live-trading-ready quantitative trading framework for developing algorithm modules independently or collaboratively.
`Ada` is short for `Adapter`, and also pays tribute to computing pioneer **Ada Lovelace** and the Ada programming language named after her.

QuantAda pushes back against the overfitting and guru culture common in quantitative trading. It brings the focus back to disciplined engineering, sound mathematical reasoning, and respect for markets.
Its central idea is to decouple strategies, data providers, risk controls, and broker execution through adapters, keeping execution paths clear, auditable, and recoverable.

## Quick Start

### 1. Install

```bash
git clone https://github.com/SUTFutureCoder/QuantAda.git
cd QuantAda

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

Configure at least one data-provider token in `config.py` (commonly `TUSHARE_TOKEN`):

```python
TUSHARE_TOKEN = "your_token_here"
```

Optionally enable database recording:

```python
DB_ENABLED = True
DB_URL = "sqlite:///quantada_logs.db"
```

### 3. Basic Backtest

```bash
python run.py sample_macd_cross_strategy --symbols=SHSE.600519
python run.py --help
```

### 4. Common Commands

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
```

### 5. Parameter Optimization (Optuna)

```bash
# Enter optimization mode
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}"

# Set explicit training and test periods
python run.py sample_macd_cross_strategy --symbols=SHSE.600519 --opt_params "{'fast_period': {'type': 'int', 'low': 5, 'high': 30}}" --train_period 20210101-20221231 --test_period 20230101-20231231 --n_trials 50
```

### 6. Connect to Live Trading or Simulation

Configure `BROKER_ENVIRONMENTS` in `config.py`, then launch with `--connect=broker:env`:

```bash
python run.py sample_macd_cross_strategy --connect=gm_broker:sim
python run.py sample_macd_cross_strategy --connect=gm_broker:real
python run.py sample_macd_cross_strategy --connect=ib_broker:sim
```

### 7. SDK / Plugin Mode (Strategies Outside This Repository)

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

This project is intended solely for technical research and engineering practice. It does not constitute investment advice.
Live trading involves a risk of financial loss. Perform thorough backtesting and simulation before deployment.
You are solely responsible for any losses resulting from use of this project.

## Author

- Blog: [project256.com](https://project256.com)
- GitHub: [SUTFutureCoder](https://github.com/SUTFutureCoder)

## License

MIT
