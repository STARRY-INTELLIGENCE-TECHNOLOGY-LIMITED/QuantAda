"""优化器包的公开 API。

运行时实现位于 optimizer.runtime。本包级模块保持 CLI 导入简洁，
同时避免恢复旧的 common.* 归属关系。
"""

__all__ = [
    "OptimizationJob",
    "get_metric_function",
    "is_port_in_use",
    "run_optimizer_mode",
]


def __getattr__(name):
    """浏览训练历史时保持轻量，仅在调用运行期 API 时加载科学计算依赖。"""
    if name not in __all__:
        raise AttributeError(name)
    from . import runtime

    value = getattr(runtime, name)
    globals()[name] = value
    return value
