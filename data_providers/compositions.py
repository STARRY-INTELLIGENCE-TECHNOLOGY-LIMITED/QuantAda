"""按配置构造 Provider 组合；不包含具体市场或券商逻辑。"""

from __future__ import annotations

import importlib
from collections.abc import Mapping


_PROVIDER_ALIASES = {
    'thetadata': 'theta',
    'futu_broker': 'futu',
    'gm_broker': 'gm',
    'ibkr': 'ibkr',
    'ib_broker': 'ibkr',
    'sxsc_tushare': 'sxsctushare',
}


def _load_factory(reference):
    """加载 ``package.module:factory`` 形式的组合工厂。"""

    text = str(reference or '').strip()
    if not text:
        raise ValueError('Provider composition factory is empty')
    module_name, separator, attribute = text.partition(':')
    if not separator or not module_name or not attribute:
        raise ValueError(f'Invalid Provider composition factory: {reference!r}')
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError(f'Provider composition factory is not callable: {reference!r}')
    return factory


def build_provider_composition(alias, spec, provider_map):
    """根据配置把已发现的 Provider 注入具体组合工厂。"""

    if not isinstance(spec, Mapping):
        raise ValueError(f'Invalid Provider composition spec: {alias!r}')
    historical_name = _PROVIDER_ALIASES.get(
        str(spec.get('historical') or '').strip().lower(),
        str(spec.get('historical') or '').strip().lower(),
    )
    realtime_name = _PROVIDER_ALIASES.get(
        str(spec.get('realtime') or '').strip().lower(),
        str(spec.get('realtime') or '').strip().lower(),
    )
    historical = provider_map.get(historical_name)
    realtime = provider_map.get(realtime_name)
    if historical is None:
        raise ValueError(f'Historical Provider is unavailable: {historical_name}')
    if realtime is None:
        raise ValueError(f'Realtime Provider is unavailable: {realtime_name}')
    factory = _load_factory(spec.get('factory'))
    factory_kwargs = {}
    configured_kwargs = spec.get('factory_kwargs', {})
    if configured_kwargs:
        if not isinstance(configured_kwargs, Mapping):
            raise ValueError(f'Invalid Provider composition factory_kwargs: {alias!r}')
        for argument, source in configured_kwargs.items():
            source_name = str(source or '').strip().lower()
            if source_name == 'historical':
                factory_kwargs[str(argument)] = historical
            elif source_name == 'realtime':
                factory_kwargs[str(argument)] = realtime
            else:
                raise ValueError(f'Unknown Provider composition source: {source!r}')
    else:
        factory_kwargs = {
            'historical_provider': historical,
            'realtime_provider': realtime,
        }
    factory_options = spec.get('factory_options', {})
    if factory_options:
        if not isinstance(factory_options, Mapping):
            raise ValueError(f'Invalid Provider composition factory_options: {alias!r}')
        factory_kwargs.update({str(key): value for key, value in factory_options.items()})
    try:
        return factory(**factory_kwargs)
    except TypeError:
        # 兼容当前 Theta/Futu 适配器的历史参数名；新组合应在配置中
        # 明确声明 factory_kwargs，避免依赖该回退。
        if not configured_kwargs:
            return factory(theta_provider=historical, futu_provider=realtime)
        raise


__all__ = ['build_provider_composition']
