import threading

import pandas as pd

import config
import data_providers.futu_provider as futu_module
from data_providers.akshare_provider import AkshareDataProvider
from data_providers.futu_provider import FutuDataProvider


def test_futu_provider_priority_leaves_akshare_as_default_fallback():
    assert FutuDataProvider.PRIORITY == 70
    assert AkshareDataProvider.PRIORITY == 80


class FakeQuoteContext:
    def __init__(self, history=None, option_chain=None, snapshot=None, basicinfo=None):
        self.history = history
        self.option_chain = option_chain
        self.snapshot = snapshot
        self.basicinfo = basicinfo
        self.history_calls = []
        self.option_chain_calls = []
        self.snapshot_calls = []
        self.basicinfo_calls = []
        self.closed = False
        self.timeout = None

    def set_sync_query_connect_timeout(self, timeout):
        self.timeout = timeout

    def request_history_kline(self, code, **kwargs):
        self.history_calls.append((code, kwargs))
        return 0, self.history, None

    def get_option_chain(self, code, **kwargs):
        self.option_chain_calls.append((code, kwargs))
        return 0, self.option_chain

    def get_market_snapshot(self, codes):
        self.snapshot_calls.append(codes)
        return 0, self.snapshot

    def get_stock_basicinfo(self, **kwargs):
        self.basicinfo_calls.append(kwargs)
        return 0, self.basicinfo

    def close(self):
        self.closed = True


def _history_frame():
    return pd.DataFrame(
        {
            'code': ['SH.600519', 'SH.600519', 'SH.600519'],
            'name': ['Moutai', 'Moutai', 'Moutai'],
            'time_key': ['2024-01-03', '2024-01-01', '2024-01-01'],
            'open': [1800.0, 1780.0, 1781.0],
            'close': [1810.0, 1790.0, 1791.0],
            'high': [1820.0, 1800.0, 1801.0],
            'low': [1790.0, 1770.0, 1771.0],
            'volume': [300, 100, 101],
            'turnover': [540000.0, 179000.0, 179100.0],
        }
    )


def test_futu_normalizes_a_share_and_international_symbols():
    normalize = FutuDataProvider._normalize_symbol
    assert normalize('SHSE.600519') == 'SH.600519'
    assert normalize('SZSE.000001') == 'SZ.000001'
    assert normalize('HK.00700') == 'HK.00700'
    assert normalize('HK.700') == 'HK.00700'
    assert normalize('NASDAQ.AAPL') == 'US.AAPL'
    assert normalize('AAPL.SMART') == 'US.AAPL'
    assert normalize('BRK.B') == 'US.BRK.B'
    assert normalize('US.BRK.B') == 'US.BRK.B'
    assert normalize('US.AAPL.USD') == 'US.AAPL'
    assert normalize('US.BRK.B.NASDAQ.USD') == 'US.BRK.B'
    assert normalize('STK.BRK.B.USD') == 'US.BRK.B'
    assert normalize('BRK.B.NASDAQ') == 'US.BRK.B'
    assert normalize('US.AAPL.SMART') == 'US.AAPL'
    assert normalize('SEHK.00700') == 'HK.00700'
    assert normalize('CRYPTO.BTC.USD') == 'CRYPTO.BTC.USD'
    assert normalize('STK.AAPL.USD') == 'US.AAPL'
    assert normalize('AAPL') == 'US.AAPL'
    assert normalize('00700') == 'HK.00700'
    assert normalize('700') == 'HK.00700'


def test_futu_get_data_standardizes_history_and_preserves_metadata():
    context = FakeQuoteContext(history=_history_frame())
    provider = FutuDataProvider(quote_ctx=context)

    result = provider.get_data(
        'SHSE.600519',
        start_date='20240101',
        end_date='20240103',
    )

    assert result is not None
    assert list(result.index) == [pd.Timestamp('2024-01-01'), pd.Timestamp('2024-01-03')]
    assert list(result.columns[:5]) == ['open', 'high', 'low', 'close', 'volume']
    assert result.loc[pd.Timestamp('2024-01-01'), 'close'] == 1791.0
    assert result.loc[pd.Timestamp('2024-01-03'), 'close'] == 1810.0
    code, kwargs = context.history_calls[0]
    assert code == 'SH.600519'
    assert kwargs['start'] == '2024-01-01'
    assert kwargs['end'] == '2024-01-03'
    assert kwargs['autype'] == getattr(futu_module.AuType, 'QFQ', 'qfq')
    assert kwargs['max_count'] is None


def test_futu_intraday_request_preserves_seconds_and_uses_native_period():
    context = FakeQuoteContext(history=pd.DataFrame(
        {
            'time_key': ['2024-01-02 09:30:00'],
            'open': [10.0],
            'high': [10.2],
            'low': [9.9],
            'close': [10.1],
            'volume': [1000],
        }
    ))
    provider = FutuDataProvider(quote_ctx=context)

    result = provider.get_data(
        'US.AAPL',
        start_date='2024-01-02 09:30:00',
        end_date='2024-01-02',
        timeframe='Minutes',
        compression=5,
    )

    assert result is not None and not result.empty
    code, kwargs = context.history_calls[0]
    assert code == 'US.AAPL'
    assert kwargs['ktype'] == getattr(futu_module.KLType, 'K_5M', 'K_5M')
    assert kwargs['start'] == '2024-01-02 09:30:00'
    assert kwargs['end'] == '2024-01-02 23:59:59'

    provider.get_data(
        'US.AAPL',
        start_date='2024-01-02 09:30:00',
        end_date='2024-01-02 00:00:00',
        timeframe='Minutes',
        compression=5,
    )
    assert context.history_calls[1][1]['end'] == '2024-01-02 00:00:00'


def test_futu_provider_rebuilds_closed_quote_context_after_disconnect(monkeypatch):
    """行情上下文断线进入 CLOSED 后不能永久复用旧句柄。"""
    created = []

    class ClosedQuote:
        status = 'CLOSED'

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class ReadyQuote(FakeQuoteContext):
        status = 'READY'

    stale = ClosedQuote()

    def open_quote(**_kwargs):
        fresh = ReadyQuote(history=_history_frame())
        created.append(fresh)
        return fresh

    monkeypatch.setattr(futu_module, 'OpenQuoteContext', open_quote)
    provider = FutuDataProvider(quote_ctx=stale)
    provider._owns_quote_ctx = True

    fresh = provider._get_quote_context()

    assert stale.closed is True
    assert created == [fresh]
    assert provider._quote_ctx is fresh
    assert provider._owns_quote_ctx is True


def test_futu_provider_does_not_spawn_parallel_context_threads_after_timeout(monkeypatch):
    """OpenD 构造超时后应等待原线程结束并进入短暂退避，不能并发创建连接。"""
    started = threading.Event()
    release = threading.Event()
    created = []

    class SlowQuote:
        status = 'READY'

        def close(self):
            return None

    def open_quote(**_kwargs):
        started.set()
        release.wait(0.2)
        context = SlowQuote()
        created.append(context)
        return context

    monkeypatch.setattr(futu_module, 'OpenQuoteContext', open_quote)
    monkeypatch.setattr(futu_module, '_CONTEXT_INIT_TIMEOUT_SECONDS', 0.01)
    provider = FutuDataProvider()

    assert provider._get_quote_context() is None
    assert started.wait(1.0)
    first_thread = provider._quote_context_init_thread
    assert first_thread is not None and first_thread.is_alive()

    assert provider._get_quote_context() is None
    assert provider._quote_context_init_thread is first_thread
    assert created == []

    release.set()
    first_thread.join(1.0)
    assert not first_thread.is_alive()


def test_futu_option_history_uses_unadjusted_prices():
    context = FakeQuoteContext(
        history=pd.DataFrame({
            'time_key': ['2024-01-02'],
            'open': [1.0],
            'high': [1.2],
            'low': [0.9],
            'close': [1.1],
            'volume': [100],
        }),
        snapshot=pd.DataFrame([{
            'code': 'US.AAPL210115C185000',
            'option_contract_multiplier': 100.0,
        }]),
    )
    provider = FutuDataProvider(quote_ctx=context)

    result = provider.get_data('US.AAPL210115C185000')

    assert result is not None
    assert context.history_calls[0][1]['autype'] == getattr(futu_module.AuType, 'NONE', 'None')


def test_futu_option_history_falls_back_to_event_contract_api_and_keeps_multiplier():
    class EventContractContext(FakeQuoteContext):
        def __init__(self):
            super().__init__(history=None, snapshot=pd.DataFrame([
                {
                    'code': 'US.AAPL210115C185000',
                    'option_contract_multiplier': 100.0,
                }
            ]))
            self.event_history_calls = []

        def request_history_kline(self, code, **kwargs):
            self.history_calls.append((code, kwargs))
            return 1, 'unknown stock', None

        def request_history_event_contract_kline(self, code, **kwargs):
            self.event_history_calls.append((code, kwargs))
            return 0, pd.DataFrame({
                'time_key': ['2024-01-02'],
                'open': [1.0],
                'high': [1.2],
                'low': [0.9],
                'close': [1.1],
                'volume': [100.0],
            }), None

    context = EventContractContext()
    provider = FutuDataProvider(quote_ctx=context)

    result = provider.get_data('US.AAPL210115C185000')

    assert result is not None
    assert len(context.history_calls) == 1
    assert len(context.event_history_calls) == 1
    assert context.event_history_calls[0][0] == 'US.AAPL210115C185000'
    assert result.attrs['contract_multiplier'] == 100.0
    assert result['contract_multiplier'].iloc[-1] == 100.0


def test_futu_option_history_without_multiplier_is_excluded():
    context = FakeQuoteContext(history=pd.DataFrame({
        'time_key': ['2024-01-02'],
        'open': [1.0],
        'high': [1.2],
        'low': [0.9],
        'close': [1.1],
        'volume': [100.0],
    }))
    provider = FutuDataProvider(quote_ctx=context)

    assert provider.get_data('US.AAPL210115C185000') is None


def test_futu_option_history_uses_multiplier_from_history_when_snapshot_unavailable():
    history = pd.DataFrame({
        'time_key': ['2024-01-02'],
        'open': [1.0],
        'high': [1.2],
        'low': [0.9],
        'close': [1.1],
        'volume': [100.0],
        'option_contract_size': [100.0],
    })
    context = FakeQuoteContext(history=history, snapshot=None)
    provider = FutuDataProvider(quote_ctx=context)

    result = provider.get_data('US.AAPL210115C185000')

    assert result is not None
    assert result.attrs['contract_multiplier'] == 100.0
    assert result['contract_multiplier'].iloc[-1] == 100.0
    assert context.snapshot_calls == []


def test_futu_derivative_history_uses_unadjusted_prices():
    context = FakeQuoteContext(history=pd.DataFrame(
        {
            'time_key': ['2024-01-02'],
            'open': [100.0],
            'high': [101.0],
            'low': [99.0],
            'close': [100.5],
            'volume': [10],
        }
    ))
    provider = FutuDataProvider(quote_ctx=context)

    assert provider.get_data('HK_FUTURE.202401', end_date='2024-01-02') is not None
    assert context.history_calls[0][1]['autype'] == getattr(futu_module.AuType, 'NONE', 'None')


def test_futu_option_chain_records_contracts_for_future_history_calls():
    chain = pd.DataFrame(
        {
            'code': ['US.AAPL210115C185000'],
            'strike_time': ['2021-01-15'],
            'strike_price': [185.0],
        }
    )
    history = pd.DataFrame(
        {
            'time_key': ['2024-01-02'],
            'open': [1.0],
            'high': [1.2],
            'low': [0.9],
            'close': [1.1],
            'volume': [100],
        }
    )
    context = FakeQuoteContext(
        history=history,
        option_chain=chain,
        snapshot=pd.DataFrame([{
            'code': 'US.AAPL210115C185000',
            'option_contract_multiplier': 100.0,
        }]),
    )
    provider = FutuDataProvider(quote_ctx=context)

    result = provider.get_option_chain('NASDAQ.AAPL', start='2024-01-01', end='2024-01-03')
    provider.get_data('US.AAPL210115C185000')

    assert result is not None
    assert context.option_chain_calls[0][0] == 'US.AAPL'
    assert context.option_chain_calls[0][1]['start'] == '2024-01-01'
    assert context.option_chain_calls[0][1]['end'] == '2024-01-03'
    assert context.history_calls[0][1]['autype'] == getattr(futu_module.AuType, 'NONE', 'None')


def test_futu_snapshot_and_basicinfo_normalize_codes():
    snapshot = pd.DataFrame({'code': ['SH.600519'], 'last_price': [1800.0]})
    basicinfo = pd.DataFrame({'code': ['US.AAPL'], 'stock_type': ['STOCK']})
    context = FakeQuoteContext(snapshot=snapshot, basicinfo=basicinfo)
    provider = FutuDataProvider(quote_ctx=context)

    assert provider.get_market_snapshot('SHSE.600519,AAPL') is not None
    assert context.snapshot_calls == [['SH.600519', 'US.AAPL']]
    assert provider.get_stock_basicinfo(code_list=['AAPL']) is not None
    assert context.basicinfo_calls[0]['code_list'] == ['US.AAPL']


def test_futu_rsa_path_controls_protocol_encryption_without_extra_switch(monkeypatch, tmp_path):
    calls = []

    class FakeSysConfig:
        @staticmethod
        def set_all_thread_daemon(enabled):
            calls.append(('daemon', enabled))

        @staticmethod
        def enable_proto_encrypt(enabled):
            calls.append(('encrypt', enabled))

        @staticmethod
        def set_init_rsa_file(file):
            calls.append(('rsa', file))

    class FakeOpenQuoteContext(FakeQuoteContext):
        def __init__(self, host, port, **kwargs):
            super().__init__()
            calls.append(('context', host, port, kwargs))

    key_file = tmp_path / 'futu.rsa'
    key_file.write_text('test-key', encoding='utf-8')
    monkeypatch.setattr(futu_module, 'SysConfig', FakeSysConfig)
    monkeypatch.setattr(futu_module, 'OpenQuoteContext', FakeOpenQuoteContext)
    monkeypatch.setattr(config, 'FUTU_HOST', '192.0.2.10')
    monkeypatch.setattr(config, 'FUTU_PORT', 11111)
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', str(key_file))

    provider = FutuDataProvider()
    context = provider._get_quote_context()

    assert context is not None
    assert calls[:4] == [
        ('encrypt', True),
        ('rsa', str(key_file)),
        ('daemon', True),
        ('context', '192.0.2.10', 11111, {'is_encrypt': True, 'is_async_connect': True}),
    ]
    assert context.timeout == 5.0

    calls.clear()
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')
    provider.close()
    provider = FutuDataProvider()
    context = provider._get_quote_context()

    assert context is not None
    assert calls[:3] == [
        ('encrypt', False),
        ('daemon', True),
        ('context', '192.0.2.10', 11111, {'is_encrypt': False, 'is_async_connect': True}),
    ]


def test_futu_provider_waits_for_async_context_ready(monkeypatch):
    class DelayedContext:
        def __init__(self):
            self.status_reads = 0

        @property
        def status(self):
            self.status_reads += 1
            return 'READY' if self.status_reads >= 2 else 'START'

    monkeypatch.setattr(futu_module, '_CONTEXT_INIT_TIMEOUT_SECONDS', 0.1)
    monkeypatch.setattr(futu_module.time, 'sleep', lambda _seconds: None)
    context = DelayedContext()

    assert futu_module.FutuDataProvider._wait_context_ready(context) is True
    assert context.status_reads >= 2


def test_futu_provider_closes_only_contexts_it_owns():
    injected = FakeQuoteContext()
    provider = FutuDataProvider(quote_ctx=injected)
    provider.close()
    assert injected.closed is False

    owned = FakeQuoteContext()
    provider = FutuDataProvider(quote_ctx=None)
    provider._quote_ctx = owned
    provider.close()
    assert owned.closed is True
