"""期权实盘保护配置。"""

# 未提供风险快照能力的普通股票 Broker 不会启动 Watchdog。
OPTION_RISK_WATCHDOG_ENABLED = True
OPTION_RISK_WATCHDOG_INTERVAL_SECONDS = 60.0
# None 表示不单独限制 Gamma；保证金利用率和盘口价差仍可保护开仓。
OPTION_RISK_MAX_GAMMA = None
OPTION_RISK_MAX_MARGIN_UTILIZATION = 0.90
OPTION_RISK_MAX_SPREAD_PCT = 0.50
# 实时期权/底层报价允许的最大年龄；缺少时间戳一律视为不可信。
OPTION_RISK_MAX_QUOTE_AGE_SECONDS = 300.0
