# --- 运行时环境配置映射 ---
import ast
import os
import warnings


def _coerce_environment_value(name, raw_value, default_value):
    """按统一入口中现有默认值的类型转换环境变量。"""
    raw_text = str(raw_value)
    if isinstance(default_value, bool):
        normalized = raw_text.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError("expected a boolean")

    if isinstance(default_value, int) and not isinstance(default_value, bool):
        return int(raw_text.strip(), 10)

    if isinstance(default_value, float):
        return float(raw_text.strip())

    if isinstance(default_value, str):
        return raw_text

    if default_value is None:
        normalized = raw_text.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
        if normalized in {"", "none", "null"}:
            return None
        try:
            parsed = ast.literal_eval(raw_text)
        except (SyntaxError, ValueError):
            return raw_text
        if not isinstance(parsed, str):
            return parsed
        return raw_text

    parsed = ast.literal_eval(raw_text)
    if isinstance(default_value, dict) and not isinstance(parsed, dict):
        raise ValueError("expected a dictionary")
    if isinstance(default_value, list) and not isinstance(parsed, list):
        raise ValueError("expected a list")
    if isinstance(default_value, tuple):
        if not isinstance(parsed, (list, tuple)):
            raise ValueError("expected a list or tuple")
        return tuple(parsed)
    if isinstance(default_value, set):
        if not isinstance(parsed, (list, tuple, set)):
            raise ValueError("expected a list, tuple or set")
        return set(parsed)
    return parsed


def apply_environment_overrides():
    """将同名环境变量映射到已平铺配置；测试模式下完全跳过。"""
    if str(os.environ.get("QUANTADA_TEST_MODE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {}

    module_globals = globals()
    applied = {}
    for name, default_value in list(module_globals.items()):
        if (
            not isinstance(name, str)
            or not name.isupper()
            or name not in os.environ
        ):
            continue
        try:
            value = _coerce_environment_value(name, os.environ[name], default_value)
        except (SyntaxError, TypeError, ValueError, OverflowError) as exc:
            warnings.warn(
                f"环境变量 {name} 无法转换为配置默认类型，已保留默认值: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        module_globals[name] = value
        applied[name] = value
    if set(applied) & {
        "GM_BROKER_ENVIRONMENTS",
        "IB_BROKER_ENVIRONMENTS",
        "FUTU_BROKER_ENVIRONMENTS",
    }:
        from configs.manager import refresh_broker_environments

        module_globals["BROKER_ENVIRONMENTS"] = refresh_broker_environments()
    return applied


# --- 交易及框架基础配置 ---
# 通用数量步长；具体 Broker 可按市场覆盖（例如 Futu A 股买入强制至少 100 股）。
# 美股通常为整数 1，币市可按交易对设置为 0.00000001 等正数。
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

# 运行时数据根目录。行情 CSV 缓存在 DATA_PATH/market_cache/，日志等其它文件仍可放在根下。
# 相对路径固定到 run.py 同级，不随启动目录变化；绝对路径才是强指定。
DATA_PATH = '.data'

# 缓存数据；启用后单一显式在线 data_source 会优先复用 market_cache 中的完整 CSV。
# 使用 --refresh 可绕过缓存重新拉取并合并，显式 data_source=csv 仍可离线读取。
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


# --- 责任域配置：显式列出模块，模块内部使用 import * 平铺配置键 ---
# 这里不做目录自动扫描：配置入口保持一眼可读，新增责任域只需增加一行导入。
from configs.providers import *
from configs.alarms import *
from configs.futu import *
from configs.gm import *
from configs.ibkr import *
from configs.options import *
from configs.manager import *
from configs.optimizer import *
