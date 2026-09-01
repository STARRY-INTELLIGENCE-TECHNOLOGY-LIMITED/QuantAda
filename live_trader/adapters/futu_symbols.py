"""Futu 行情与交易共用的证券代码归一化规则。"""

import re


OPTION_CODE_RE = re.compile(
    r'^(?:US|HK|SH|SZ)\..+\d{6,8}[CP]\d+$',
    re.IGNORECASE,
)

VENUE_ALIASES = {
    'SHSE': 'SH',
    'SSE': 'SH',
    'SZSE': 'SZ',
    'HKEX': 'HK',
    'SEHK': 'HK',
    'NASDAQ': 'US',
    'NYSE': 'US',
    'AMEX': 'US',
    'ARCA': 'US',
    'IEX': 'US',
    'SMART': 'US',
    'ISLAND': 'US',
    'BATS': 'US',
    'CBOE': 'US',
    'SGX': 'SG',
    'TSE': 'JP',
    'TYO': 'JP',
    'ASX': 'AU',
    'TSX': 'CA',
}

MARKETS = {
    'SH', 'SZ', 'HK', 'US', 'SG', 'JP', 'AU', 'CA', 'MY',
    'FX', 'HK_FUTURE', 'HKCC', 'CRYPTO',
}


def _text(value, default=''):
    """将代码字段转换为去空白的大写前置文本。"""
    if value is None:
        return default
    return str(value).strip()


def normalize_futu_symbol(symbol, market_hint=None):
    """将 QuantAda、IBKR 和 Futu 代码统一为 ``MARKET.CODE``。"""
    raw = _text(symbol).upper()
    if not raw or raw in {'N/A', 'NA', 'NONE', 'NAN', 'NULL'}:
        return ''

    hint = VENUE_ALIASES.get(_text(market_hint).upper(), _text(market_hint).upper())
    if hint in {'HKCC', 'CN', 'N/A'}:
        hint = ''
    parts = raw.split('.')
    if len(parts) >= 2:
        prefix = parts[0]
        if prefix == 'US':
            symbol_parts = parts[1:]
            if len(symbol_parts) > 1 and symbol_parts[-1] in {'USD', 'HKD', 'CNY', 'CNH'}:
                symbol_parts.pop()
            if len(symbol_parts) > 1 and symbol_parts[-1] in VENUE_ALIASES:
                symbol_parts.pop()
            return f'US.{".".join(symbol_parts)}'
        if prefix == 'STK':
            currency = parts[-1] if len(parts) >= 3 else ''
            code_parts = parts[1:-1] if currency in {'USD', 'HKD', 'CNY', 'CNH'} else parts[1:]
            code = '.'.join(code_parts)
            if currency == 'HKD':
                return f'HK.{code.zfill(5)}'
            if currency in {'CNY', 'CNH'}:
                market = 'SH' if code.startswith(('5', '6', '68')) else 'SZ'
                return f'{market}.{code}'
            return f'US.{code}'
        if parts[1] in VENUE_ALIASES or parts[1] in MARKETS:
            market = VENUE_ALIASES.get(parts[1], parts[1])
            code = '.'.join(parts[2:]) if prefix in {'MARKET', 'VENUE'} else prefix
            return f'{market}.{code.zfill(5) if market == "HK" and code.isdigit() else code}'
        if parts[-1] in VENUE_ALIASES or parts[-1] in MARKETS:
            market = VENUE_ALIASES.get(parts[-1], parts[-1])
            code_parts = parts[:-1]
            leading_market = VENUE_ALIASES.get(code_parts[0], code_parts[0])
            if len(code_parts) > 1 and leading_market == market:
                code_parts = code_parts[1:]
            code = '.'.join(code_parts)
            return f'{market}.{code.zfill(5) if market == "HK" and code.isdigit() else code}'
        mapped_prefix = VENUE_ALIASES.get(prefix, prefix)
        if prefix not in MARKETS and prefix not in VENUE_ALIASES:
            return f'US.{raw}'
        code = '.'.join(parts[1:])
        if mapped_prefix == 'HK' and code.isdigit():
            code = code.zfill(5)
        return f'{mapped_prefix}.{code}'

    if hint in MARKETS or hint in VENUE_ALIASES:
        market = VENUE_ALIASES.get(hint, hint)
        return f'{market}.{raw.zfill(5) if market == "HK" and raw.isdigit() else raw}'
    if raw.isdigit() and len(raw) == 6:
        return f'{"SH" if raw.startswith(("5", "6", "68")) else "SZ"}.{raw}'
    if raw.isdigit() and 1 <= len(raw) <= 5:
        return f'HK.{raw.zfill(5)}'
    return f'US.{raw}'


__all__ = ('MARKETS', 'OPTION_CODE_RE', 'VENUE_ALIASES', 'normalize_futu_symbol')
