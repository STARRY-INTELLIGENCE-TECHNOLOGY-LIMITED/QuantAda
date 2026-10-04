"""GM Broker 的连接凭据和连接环境默认值。"""

import os

# GM SDK 的旧版 protobuf 生成代码与新版 C++ 实现不兼容；必须在任何 gm.api
# 导入前启用纯 Python protobuf 实现，历史行情接口才能正常加载。
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')

try:
    from google.protobuf.descriptor import FieldDescriptor

    if not hasattr(FieldDescriptor, 'label'):
        # GM 3.0.183 的转换器仍读取旧版 protobuf 的 label 属性；新版只保留 is_repeated。
        FieldDescriptor.label = property(
            lambda field: (
                FieldDescriptor.LABEL_REPEATED
                if getattr(field, 'is_repeated', False)
                else FieldDescriptor.LABEL_OPTIONAL
            )
        )
except Exception:
    # protobuf 不可用时由 GM Provider 的依赖检查负责报告错误。
    pass


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
