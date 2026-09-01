"""富途 OpenD 行情与交易连接配置默认值。"""


# OpenD 监听地址；可改为局域网或远端 OpenD 地址。
FUTU_HOST = '127.0.0.1'

# OpenD 默认端口。
FUTU_PORT = 11111

# RSA 私钥文件路径；为空时使用明文协议，不启用 RSA 加密。
FUTU_RSA_KEY_PATH = ''

# 交易上下文默认筛选市场；N/A 表示由 OpenD 返回当前账户可用的证券市场。
FUTU_FILTER_TRDMARKET = 'N/A'

# 默认交易环境；未显式选择 futu_broker:real 时使用模拟盘，降低误实盘风险。
FUTU_TRADE_ENV = 'SIMULATE'

# 账户路由；0 表示让 Futu SDK 按 account_index 选择默认账户。
FUTU_ACCOUNT_ID = 0
FUTU_ACCOUNT_INDEX = 0

# 富途安全机构枚举；N/A 让 SDK 自动选择可用机构。
FUTU_SECURITY_FIRM = 'N/A'

# 账户现金查询币种。
FUTU_ACCOUNT_CURRENCY = 'HKD'

# 普通调仓订单默认值。
FUTU_ORDER_TYPE = 'NORMAL'
FUTU_TIME_IN_FORCE = 'DAY'
FUTU_FILL_OUTSIDE_RTH = False

# Futu Broker 的连接环境；schedule 使用 ``sim/real``，行情订阅事件使用
# ``sim_event/real_event``。事件环境不配置 schedule，默认订阅日 K 线。
FUTU_BROKER_ENVIRONMENTS = {
    'futu_broker': {
        'sim': {
            'trd_env': 'SIMULATE',
            'schedule': '1d:14:55:00',
            'timezone': 'Asia/Shanghai',
        },
        'real': {
            'trd_env': 'REAL',
            'schedule': '1d:14:55:00',
            'timezone': 'Asia/Shanghai',
        },
        # 事件模式不设置 schedule；由 Futu CurKlineHandlerBase 推送新 K 线事件。
        'sim_event': {
            'trd_env': 'SIMULATE',
            'trigger': 'subscription',
            'event_subtype': 'K_DAY',
            'timezone': 'Asia/Shanghai',
        },
        'real_event': {
            'trd_env': 'REAL',
            'trigger': 'subscription',
            'event_subtype': 'K_DAY',
            'timezone': 'Asia/Shanghai',
        },
    },
}
