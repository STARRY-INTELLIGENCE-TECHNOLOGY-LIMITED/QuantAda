"""
Optimizer package public API.

The runtime implementation lives in optimizer.runtime. This package-level
module keeps the CLI import compact while avoiding the old common.* ownership.
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
