"""优化器运行期配置。"""

# worker 配额以物理可用内存为主，并额外保留该比例的物理余量；swap 只按有界比例计入。
# 用户可通过 config.py 或 CLI --config 覆盖，例如 {'OPTIMIZER_MEMORY_RESERVE_RATIO': 0.10}。
OPTIMIZER_MEMORY_RESERVE_RATIO = 0.05
