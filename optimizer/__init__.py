"""优化器包的公开 API。

运行时实现位于 optimizer.runtime。本包级模块保持 CLI 导入简洁，
同时避免恢复旧的 common.* 归属关系。
"""

from .runtime import (
    OptimizationJob,
    get_metric_function,
    is_port_in_use,
    run_optimizer_mode,
)

__all__ = [
    "OptimizationJob",
    "get_metric_function",
    "is_port_in_use",
    "run_optimizer_mode",
]
