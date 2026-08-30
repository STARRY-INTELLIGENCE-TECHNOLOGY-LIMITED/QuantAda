"""IBKR Broker 与其数据 Provider 共用的连接默认值。"""


# TWS/Gateway 地址；本机默认连接 TWS 的 API 端口。
IBKR_HOST = '127.0.0.1'

# TWS 默认 7497；IB Gateway 常用 4001/4002，请按实际实例修改。
IBKR_PORT = 7497

# IB API clientId；同一 Gateway 中各客户端应使用不同值。
IBKR_CLIENT_ID = 0

# 可选的明确下单账户；留空时沿用 IB 默认账户路由。
IBKR_ORDER_ACCOUNT = ''

# IBKR Broker 的连接环境；连接命令保持 ``--connect=ib_broker:sim`` 兼容。
IB_BROKER_ENVIRONMENTS = {
    'ib_broker': {
        'sim': {
            'schedule': '1d:15:45:00',
            'timezone': 'America/New_York',
            # 'alarm_window': '30m:15m',
        },
        'real': {
            'schedule': '1d:15:45:00',
            'timezone': 'America/New_York',
            # 'alarm_window': '30m:15m',
        },
    },
}
