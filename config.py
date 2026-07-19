# --- 交易及框架基础配置 ---
# 股数取整（A股100，IBKR、国际市场或加密货币1）
LOT_SIZE = 100

# 年交易日，如果是加密货币请设置为365
ANNUAL_FACTOR = 252

# 数据缓存路径
DATA_PATH = '.data'

# 缓存数据，常用于离线或数据源不稳定等情况。使用后请使用--refresh或手动删除缓存目录下文件
CACHE_DATA = False

# 是否打印详细交易日志
LOG = True

# 是否打印交易计划/排名快照。
# - live: 每次计划即时推送 IM
# - backtest: 本地仍按计划打印；IM 只推送回测结束时最后一条计划/排名快照，
#   并附带执行命令、交易归因和最终绩效摘要，避免历史区间刷屏限流
PRINT_PLAN = False

# 是否跨日保留委托：
# - False: 每个交易日首次运行前自动清理所有在途委托（无状态推荐）
# - True : 保留隔日委托，不做自动清理
KEEP_OVERNIGHT_ORDERS = False

# 正式 schedule 前的轻量预热提前量：
# - 0: 关闭预热（默认）
# - 支持秒数值，或带单位字符串：'1s'、'1m'、'5m'、'1h'
# - 作用：在正式 schedule 触发前，先对第一个标的做一次轻量数据预热；
#   IB 实盘还会额外预热 USDHKD 外汇报价，降低冷连接/沉睡连接导致的首轮失败概率。
# - 注意：对于 1m/5m/15m/30m/1h 这类固定频率 schedule，预热是按正式 schedule 的下一个 slot 逆推，
#   因此该值必须严格小于 schedule 间隔，否则会自动禁用预热。
# - 用法示例：
#   LIVE_SCHEDULE_PREWARM_LEAD = '1m'
LIVE_SCHEDULE_PREWARM_LEAD = 0

# 正式 schedule 前后的报警推送时间窗：
# - 0:0 表示不限制报警窗口（默认）
# - 格式为 before:after，支持秒/分/时自由组合：
#   '30s:15m'、'5m:30s'、'30m:15m'、'1h:30m'
# - 作用：仅在正式 schedule 生效时间点附近，将报警推送到 IM；
#   超出窗口的报警本地仍会打印，但不推送到钉钉/企业微信，降低非交易时段噪音。
# - 对于固定频率 schedule（如 5m/1h），窗口同样基于正式 slot 计算。
# - 生命周期消息（STARTED/STOPPED/DEAD）与显式 plan 标签消息默认不受该窗口限制。
# - 若具体连接配置（BROKER_ENVIRONMENTS -> xxx -> conn -> alarm_window）提供该字段，
#   则连接级配置优先级更高，可覆盖这里的全局默认值。
# - 用法示例：
#   LIVE_SCHEDULE_ALARM_WINDOW = '30m:15m'
LIVE_SCHEDULE_ALARM_WINDOW = '0:0'


# --- 数据源配置 ---
# Tushare API Token
# 请到 https://tushare.pro/user/token 免费注册获取
TUSHARE_TOKEN = 'your_token_here'

# 山西证券Tushare API Token
# 用于演示快速接入数据源
SXSC_TUSHARE_TOKEN = 'your_token_here'

# 掘金API Token 格式： TOKEN[|HOST:PORT]
GM_TOKEN = 'your_token_here|host:port'

# Tiingo API Token
TIINGO_TOKEN = 'your_token_here'


# --- 报警与监控配置 ---
# 报警总开关:
# - None: 自动模式，有任一 WEBHOOK 时启用报警通道，无 WEBHOOK 时不启用
# - True: 强制启用已配置 WEBHOOK 的报警通道
# - False: 强制禁用报警通道，即使 WEBHOOK 已配置
ALARMS_ENABLED = None

# 钉钉机器人 Webhook
# 格式: 'https://oapi.dingtalk.com/robot/send?access_token=xxxx'
DINGTALK_WEBHOOK = ''

# 企业微信机器人 Webhook
# 格式: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
WECOM_WEBHOOK = ''

# 报警级别过滤: INFO, WARNING, ERROR, CRITICAL
ALARM_LEVEL = 'INFO'


def has_alarm_webhook() -> bool:
    return bool(DINGTALK_WEBHOOK or WECOM_WEBHOOK)


def is_alarms_enabled() -> bool:
    if ALARMS_ENABLED is None:
        return has_alarm_webhook()
    return bool(ALARMS_ENABLED)


# --- 数据库记录配置 ---
# 是否开启数据库记录
DB_ENABLED = False

# 数据库连接字符串
# 格式: dialect+driver://username:password@host:port/database
# 示例 (MySQL): 'mysql+pymysql://root:123456@localhost:3306/quantada_db'
# 示例 (SQLite): 'sqlite:///quantada_logs.db'
DB_URL = 'mysql+pymysql://root:yourpassword@localhost:3306/quant'


# --- 机器学习优化器配置 ---
# 参数优化实时看板端口
OPTUNA_DASHBOARD_PORT = 8090


# --- IBKR配置 ---
IBKR_HOST = '127.0.0.1'
# 文件-全局配置-API-设置-启用套接字客户端&关闭只读API, 实盘与模拟端口不一样，IB Gateway默认4001
IBKR_PORT = 7497
IBKR_CLIENT_ID = 0
# 可选: 指定下单账户（如子账户 U1234567）；留空则由 IB 默认路由到主账户。
IBKR_ORDER_ACCOUNT = ''


# --- 框架 → 实盘/仿真连接通道配置 ---
# BROKER_ENVIRONMENTS 的 Key 必须与 adapters 下的文件名一致 (如 gm_broker.py -> "gm_broker")
BROKER_ENVIRONMENTS = {
    "gm_broker": {
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
        }
    },

    # IB 配置
    "ib_broker": {
        'sim': {  # 模拟盘/Paper Trading
            # IB 当前也支持:
            # - 1d:15:45:00
            # - 5m:09:30:00
            # - 1h:09:30:00
            'schedule': '1d:15:45:00',
            'timezone': 'America/New_York',
            # 'alarm_window': '30m:15m',
        },
        'real': {  # 实盘/Docker Gateway
            'schedule': '1d:15:45:00',
            'timezone': 'America/New_York',
            # 'alarm_window': '30m:15m',
        }
    }
}
