import importlib
import os
import re

import pandas as pd

# 动态获取 Python 安装目录，并构建 Tcl/Tk 库路径
python_install_dir = os.path.dirname(os.path.dirname(os.__file__))
tcl_library_path = os.path.join(python_install_dir, 'tcl', 'tcl8.6')
tk_library_path = os.path.join(python_install_dir, 'tcl', 'tk8.6')

# 设置环境变量
os.environ['TCL_LIBRARY'] = tcl_library_path
os.environ['TK_LIBRARY'] = tk_library_path


def pascal_to_snake(name: str) -> str:
    """
    将 PascalCase (大驼峰) 字符串转换为 snake_case (下划线) 字符串。
    例如: 'SampleMacdCrossStrategy' -> 'sample_macd_cross_strategy'
    """
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def get_class_from_name(name_string: str, search_paths: list):
    """
    根据给定的名称字符串（文件名或类名）动态导入类。

    支持两种模式:
    1. 内部模式 (无点号): e.g., 'sample_macd_cross_strategy'
       - 在框架的 'search_paths' (如 'strategies/') 中查找。
    2. 外部模式 (有点号): e.g., 'my_external_strategies.my_strategy.MyStrategyClass'
       - 直接从 PYTHONPATH 导入，忽略 'search_paths'。

    :param name_string: 文件名/类名 (e.g., 'sample_macd_cross_strategy') 或
                        全限定名 (e.g., 'my_strategies.my_strategy_file.MyStrategyClass')
    :param search_paths: 内部搜索的目录列表, e.g., ['stock_selectors', 'strategies']
    :return: 动态导入的类
    """
    name_string = name_string.replace('.py', '')

    # 1. 检查是否为全限定路径 (包含点号)
    if '.' in name_string:
        try:
            # 尝试 Case 1: 'my_package.my_module.MyClass'
            # 假设用户提供了模块和类的全名
            module_path, class_name = name_string.rsplit('.', 1)
            module = importlib.import_module(module_path)

            # 获取目标对象
            obj = getattr(module, class_name)

            # 防御 Python 子模块缓存陷阱
            # 如果拿到的是个 module 而不是 class，强制抛错进入 Case 2 重新解析
            import inspect
            if inspect.ismodule(obj):
                raise ValueError(f"Attribute '{class_name}' resolves to a module, expecting a class.")

            return obj
        except (ImportError, AttributeError, ValueError) as e_class:
            # 导入失败，尝试 Case 2
            # 情况 2：'my_package.my_module_file'（snake_case）。
            # 假设用户提供了模块名，我们推断类名 (e.g., MyModuleFile)
            try:
                module_name = name_string
                class_name_base = module_name.split('.')[-1]
                class_name = "".join(word.capitalize() for word in class_name_base.split('_'))

                module = importlib.import_module(module_name)
                return getattr(module, class_name)
            except (ImportError, AttributeError) as e_module:
                # 两次尝试都失败
                raise ImportError(
                    f"Could not import '{name_string}' as a fully qualified path. \n"
                    f"  Attempt 1 (as ...MyClass) failed: {e_class} \n"
                    f"  Attempt 2 (as ...my_module) failed: {e_module}"
                )

    # 2. 原始逻辑 (如果 name_string 不含点号，则在内部搜索)
    # 启发式判断输入格式
    if '_' in name_string or name_string.islower():
        # 认为是 snake_case 文件名
        module_name = name_string
        class_name = "".join(word.capitalize() for word in module_name.split('_'))
    else:
        # 认为是 PascalCase 类名
        class_name = name_string
        module_name = pascal_to_snake(class_name)

    # 遍历搜索路径尝试导入
    for path in search_paths:
        try:
            module_path = f'{path}.{module_name}'
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except (ImportError, AttributeError):
            # 如果在一个路径中找不到，继续在下一个路径中寻找
            continue

    # 如果所有路径都尝试完毕仍未找到，则抛出异常
    raise ImportError(
        f"Could not find class '{class_name}' from module '{module_name}' "
        f"derived from input '{name_string}' in any of the search paths: {search_paths}"
    )


def parse_period_string(period_str):
    """
    解析时间周期字符串，如 '1y', '6m', '2w', '30d'
    返回 pd.DateOffset 对象
    """
    if not period_str:
        return None

    match = re.match(r"(\d+)([ymwd])", period_str.lower())
    if not match:
        raise ValueError(f"Invalid period format: {period_str}. Use format like '1y', '6m', '30d'.")

    val = int(match.group(1))
    unit = match.group(2)

    if unit == 'y':
        return pd.DateOffset(years=val)
    elif unit == 'm':
        return pd.DateOffset(months=val)
    elif unit == 'w':
        return pd.DateOffset(weeks=val)
    elif unit == 'd':
        return pd.DateOffset(days=val)
    return None
