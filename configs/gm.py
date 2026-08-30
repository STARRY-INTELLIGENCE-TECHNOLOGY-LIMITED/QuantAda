"""GM Broker 的连接凭据和连接环境默认值。"""


# GM SDK Token，支持 ``token|host:port`` 格式；连接环境中的 token 优先用于实盘启动。
GM_TOKEN = 'your_token_here|host:port'

# GM Broker 的连接环境；连接命令保持 ``--connect=gm_broker:sim`` 兼容。
GM_BROKER_ENVIRONMENTS = {
    'gm_broker': {
        'sim': {
            'strategy_id': 'xxx',
            'token': 'xxx',
            'serv_addr': '127.0.0.1:7001',
            # 支持:
            # - 1d:14:45:00   每日固定时刻
            # - 5m:09:30:00   以 09:30:00 为 anchor，每 5 分钟一个 slot
            # - 1h:09:30:00   以 09:30:00 为 anchor，每 1 小时一个 slot
            'schedule': '1d:14:45:00',
            # 可选：覆盖 LIVE_SCHEDULE_ALARM_WINDOW
            # 'alarm_window': '30m:15m',
        },
        'real': {
            'strategy_id': 'xxx',
            'token': 'xxx',
            'serv_addr': '127.0.0.1:7001',
            'schedule': '1d:14:45:00',
            # 'alarm_window': '30m:15m',
        },
    },
}
