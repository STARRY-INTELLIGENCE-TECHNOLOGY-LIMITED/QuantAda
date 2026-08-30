"""报警通道配置默认值。"""


# None=有 webhook 时自动启用，True=强制启用，False=强制禁用。
ALARMS_ENABLED = None

# 钉钉机器人 Webhook；留空表示未配置。
DINGTALK_WEBHOOK = ''

# 企业微信机器人 Webhook；留空表示未配置。
WECOM_WEBHOOK = ''

# 报警级别过滤：INFO、WARNING、ERROR、CRITICAL。
ALARM_LEVEL = 'INFO'
