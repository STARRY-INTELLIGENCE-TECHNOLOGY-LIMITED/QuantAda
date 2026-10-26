"""数据 Provider 的公共凭据默认值。"""

import os


# Tushare Pro API 令牌；未配置时 Provider 会安全跳过。
TUSHARE_TOKEN = 'your_token_here'

# 山西证券 Tushare 代理 API 令牌；未配置时 Provider 会安全跳过。
SXSC_TUSHARE_TOKEN = 'your_token_here'

# Tiingo API 令牌；未配置时 Provider 会安全跳过。
TIINGO_TOKEN = 'your_token_here'

# Thetadata API 令牌；未配置时 Provider 会安全跳过。
THETADATA_TOKEN = 'your_token_here'

# SEC 公共数据接口要求 User-Agent 标明应用和可联系邮箱；未设置时跳过在线请求。
SEC_USER_AGENT = os.getenv('SEC_USER_AGENT', 'QuantAda@starryint.hk').strip()

# OCC 调整合约归档；官方 CSV/JSON 地址或本地归档路径由用户显式提供。
OCC_ACTIONS_URL = os.getenv('OCC_ACTIONS_URL', '').strip()
OCC_ACTIONS_PATH = os.getenv('OCC_ACTIONS_PATH', '').strip()

# 财报每日点时归档；默认写入 DATA_PATH/market_cache/earnings/calendar.csv。
EARNINGS_ARCHIVE_PATH = os.getenv('EARNINGS_ARCHIVE_PATH', '').strip()

# Provider 组合配置。DataManager 只负责按名称注入已发现的 Provider，
# 代码映射、字段合并和实时新鲜度由 factory 指向的适配器负责。
DATA_PROVIDER_COMPOSITIONS = {
    'hybrid': {
        'historical': 'theta',
        'realtime': 'futu',
        'factory': 'data_providers.hybrid_provider:HybridDataProvider',
        'factory_kwargs': {
            'theta_provider': 'historical',
            'futu_provider': 'realtime',
        },
    },
}

# 将便于记忆的 data_source 名称映射为单个 Provider 或 Provider 组合表达式。
DATA_PROVIDER_ALIASES = {
    # 核心回测只加载行情与 V/W；高容量/辅助源通过显式接口调用。
    'theta_futu_sec': 'thetadata+futu,sec',
    'theta_futu_cboe': 'thetadata+futu,cboe',
    'theta_futu_research': 'thetadata+futu,sec,earnings,rates',
    'us_options_research': 'thetadata+futu,sec,cboe',
}
