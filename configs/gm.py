"""GM Broker 的连接凭据和连接环境默认值。"""

import os
import weakref

# GM SDK 的旧版 protobuf 生成代码与新版 C++ 实现不兼容；必须在任何 gm.api
# 导入前启用纯 Python protobuf 实现，历史行情接口才能正常加载。
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')

_MISSING_FIELD_LABEL = object()
_STORED_FIELD_LABELS = weakref.WeakKeyDictionary()


def install_protobuf_field_label_compat(descriptor_cls):
    """给缺少 label 的 FieldDescriptor 补上可读写属性。旧版构造器仍会执行 self.label = label。"""
    if hasattr(descriptor_cls, 'label'):
        return False

    def _get_label(field):
        stored = getattr(field, '_quantada_field_label', _MISSING_FIELD_LABEL)
        if stored is not _MISSING_FIELD_LABEL:
            return stored
        try:
            return _STORED_FIELD_LABELS[field]
        except Exception:
            pass
        repeated = getattr(descriptor_cls, 'LABEL_REPEATED', 3)
        optional = getattr(descriptor_cls, 'LABEL_OPTIONAL', 1)
        if getattr(field, 'is_repeated', False):
            return repeated
        return optional

    def _set_label(field, value):
        try:
            object.__setattr__(field, '_quantada_field_label', value)
        except Exception:
            _STORED_FIELD_LABELS[field] = value

    descriptor_cls.label = property(_get_label, _set_label)
    return True


try:
    from google.protobuf.descriptor import FieldDescriptor

    # protobuf 3.20 的 label 只在构造器里赋值，类上 hasattr 仍为 False。
    # 只读 property 会把 self.label = label 变成 can't set attribute。
    install_protobuf_field_label_compat(FieldDescriptor)
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
