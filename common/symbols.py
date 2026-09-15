"""交易代码身份匹配。

只把已知 IBKR venue 后缀视为同一标的别名，例如 ``AAPL.SMART`` 与 ``AAPL``。
Futu/GM 的 ``HK.00700`` / ``SHSE.600519`` 是市场前缀，不得把 ``HK`` / ``SHSE`` 当成共享别名。
"""

VENUE_SUFFIXES = frozenset({
    'SMART', 'ISLAND', 'NASDAQ', 'NYSE', 'AMEX', 'ARCA', 'BATS', 'PINK',
    'IEX', 'CBOE', 'MEMX', 'EDGX', 'EDGEA', 'BYX', 'BEX', 'NYSENAT',
    'OTC', 'OTCBB',
})


def normalize_symbol_key(value) -> str:
    """把 data 对象或原始代码规范为大写查询键。"""
    if hasattr(value, '_name'):
        value = getattr(value, '_name', '')
    return str(value or '').strip().upper()


def strip_known_venue_suffix(symbol) -> str:
    """去掉末尾已知 venue 后缀；市场前缀代码保持原样。"""
    normalized = normalize_symbol_key(symbol)
    if '.' not in normalized:
        return normalized
    ticker, _separator, suffix = normalized.rpartition('.')
    if ticker and suffix in VENUE_SUFFIXES:
        return ticker
    return normalized


def symbol_lookup_keys(symbol) -> tuple:
    """返回可用于索引同一证券的键：精确代码，以及去掉 venue 后缀后的代码。"""
    exact = normalize_symbol_key(symbol)
    if not exact:
        return ()
    stripped = strip_known_venue_suffix(exact)
    if stripped != exact:
        return (exact, stripped)
    return (exact,)


def symbols_match(left, right) -> bool:
    """判断两个代码是否指向同一证券；市场前缀不同的标的不会被当成同一个。"""
    left_keys = symbol_lookup_keys(left)
    right_keys = symbol_lookup_keys(right)
    if not left_keys or not right_keys:
        return False
    return not set(left_keys).isdisjoint(right_keys)
