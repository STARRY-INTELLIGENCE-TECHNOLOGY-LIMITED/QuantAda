"""数据 Provider 的公共凭据默认值。"""


# Tushare Pro API 令牌；未配置时 Provider 会安全跳过。
TUSHARE_TOKEN = 'your_token_here'

# 山西证券 Tushare 代理 API 令牌；未配置时 Provider 会安全跳过。
SXSC_TUSHARE_TOKEN = 'your_token_here'

# Tiingo API 令牌；未配置时 Provider 会安全跳过。
TIINGO_TOKEN = 'your_token_here'

# Thetadata API 令牌；未配置时 Provider 会安全跳过。
THETADATA_TOKEN = 'your_token_here'

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
