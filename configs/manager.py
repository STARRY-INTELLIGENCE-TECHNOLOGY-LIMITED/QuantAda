"""Broker 环境合并和报警状态判断。

根配置由 ``config.py`` 按责任域分组导入；本模块不扫描目录，也不创建 SDK client。
"""

import sys
from copy import deepcopy

from .alarms import ALARMS_ENABLED, DINGTALK_WEBHOOK, WECOM_WEBHOOK
from .futu import FUTU_BROKER_ENVIRONMENTS
from .gm import GM_BROKER_ENVIRONMENTS
from .ibkr import IB_BROKER_ENVIRONMENTS


def _configured_environment(name, default):
    """读取根配置中可能被用户覆盖的责任域环境字典。"""
    root = sys.modules.get('config')
    if root is not None:
        namespace = vars(root)
        if name in namespace:
            return namespace[name]
    return default


def _build_broker_environments() -> dict:
    """合并各 Broker 自己声明的连接环境。"""
    merged = {}
    for name, default in (
        ('GM_BROKER_ENVIRONMENTS', GM_BROKER_ENVIRONMENTS),
        ('IB_BROKER_ENVIRONMENTS', IB_BROKER_ENVIRONMENTS),
        ('FUTU_BROKER_ENVIRONMENTS', FUTU_BROKER_ENVIRONMENTS),
    ):
        environments = _configured_environment(name, default)
        if not isinstance(environments, dict):
            raise RuntimeError('BROKER_ENVIRONMENTS must be a dict')
        for broker_name, broker_config in environments.items():
            if broker_name in merged:
                raise RuntimeError(f'Duplicate broker environment config: {broker_name}')
            merged[broker_name] = deepcopy(broker_config)
    return merged


BROKER_ENVIRONMENTS = _build_broker_environments()


def refresh_broker_environments() -> dict:
    """按当前根配置重新合并 Broker 环境，供 CLI 覆盖后同步聚合入口。"""
    global BROKER_ENVIRONMENTS
    BROKER_ENVIRONMENTS = _build_broker_environments()
    return BROKER_ENVIRONMENTS


def _config_namespace() -> dict:
    """返回最终的根配置命名空间；独立导入时回退到本模块。"""
    root = sys.modules.get('config')
    return vars(root) if root is not None else globals()


def has_alarm_webhook() -> bool:
    """检查最终配置中的报警 webhook。"""
    namespace = _config_namespace()
    return bool(namespace.get('DINGTALK_WEBHOOK') or namespace.get('WECOM_WEBHOOK'))


def is_alarms_enabled() -> bool:
    """根据最终配置判断是否启用报警通道。"""
    namespace = _config_namespace()
    enabled = namespace.get('ALARMS_ENABLED')
    if enabled is None:
        return has_alarm_webhook()
    return bool(enabled)


__all__ = (
    'BROKER_ENVIRONMENTS',
    'has_alarm_webhook',
    'is_alarms_enabled',
)
