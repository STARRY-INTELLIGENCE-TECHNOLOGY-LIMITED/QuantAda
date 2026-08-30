# --- 交易及框架基础配置 ---
# 数量步长（A股100，美股整数1，币市可按交易对设置为0.00000001等正数）
LOT_SIZE = 1

# 实盘券商单笔订单数量上限，买卖双向生效；0 表示不限制。
# 正数按 LOT_SIZE 向下对齐，仅影响实盘拆单，回测仍保持单笔同步执行。
# 框架不另设隐藏的拆单笔数上限；请按券商真实限制配置，避免过小值产生大量子单。
BROKER_LOT_LIMITS = 0

# 单次实盘 run 的最长执行预算（秒）。所有等待、查询重试、拆单和降级共享该截止时间。
# 分钟/小时 schedule 会自动缩短为调度间隔的 80%，避免上一轮占住下一轮。
LIVE_RUN_MAX_EXECUTION_SECONDS = 600

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
# - True : 保留跨自然日委托及其本地短期跟踪；24x7 币市应使用该值
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
# - 生命周期消息（STARTED/STOPPED/DEAD）与显式 plan 标签消息默认不受此窗口限制。
# - 若具体连接配置（BROKER_ENVIRONMENTS -> xxx -> conn -> alarm_window）提供该字段，
#   则连接级配置优先级更高，可覆盖这里的全局默认值。
# - 用法示例：
#   LIVE_SCHEDULE_ALARM_WINDOW = '30m:15m'
LIVE_SCHEDULE_ALARM_WINDOW = '0:0'


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


# --- Provider 配置：历史行情 API 凭据 ---
from configs.providers import SXSC_TUSHARE_TOKEN, TIINGO_TOKEN, TUSHARE_TOKEN

# --- 报警配置：Webhook、启用策略和报警级别 ---
from configs.alarms import ALARM_LEVEL, ALARMS_ENABLED, DINGTALK_WEBHOOK, WECOM_WEBHOOK

# --- GM 配置：SDK Token；Broker 环境位于同一子配置文件 ---
from configs.gm import GM_TOKEN

# --- IBKR 配置：TWS/Gateway 连接和下单账户；Broker 环境位于同一子配置文件 ---
from configs.ibkr import IBKR_CLIENT_ID, IBKR_HOST, IBKR_ORDER_ACCOUNT, IBKR_PORT

# --- Broker 环境与报警判断：由 Manager 合并 GM/IBKR 子配置并提供状态方法 ---
from configs.manager import BROKER_ENVIRONMENTS, has_alarm_webhook, is_alarms_enabled
