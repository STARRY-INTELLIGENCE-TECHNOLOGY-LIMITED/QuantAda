from types import SimpleNamespace

import pandas as pd
import pytest

import config
from live_trader.adapters.futu_broker import FutuBrokerAdapter, FutuOrderProxy


class FakeTradeContext:
    def __init__(self):
        self.place_calls = []
        self.modify_calls = []
        self.closed = False
        self.timeout = None
        self.orders = pd.DataFrame(
            [
                {
                    'code': '600000',
                    'order_market': 'SH',
                    'trd_side': 'BUY',
                    'order_status': 'SUBMITTED',
                    'order_id': 'FUTU-1',
                    'qty': 100,
                    'dealt_qty': 25,
                }
            ]
        )

    def set_sync_query_connect_timeout(self, timeout):
        self.timeout = timeout

    def accinfo_query(self, **kwargs):
        return 0, pd.DataFrame(
            [
                {
                    'available_funds': 10000,
                    'total_assets': 12000,
                    'cash': 10000,
                    'market_val': 2000,
                }
            ]
        )

    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame(
            [
                {
                    'code': '600000',
                    'position_market': 'SH',
                    'qty': 100,
                    'can_sell_qty': 60,
                    'average_cost': 10.5,
                }
            ]
        )

    def order_list_query(self, **kwargs):
        return 0, self.orders.copy()

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return 0, pd.DataFrame(
            [
                {
                    'code': kwargs['code'],
                    'trd_side': kwargs['trd_side'],
                    'order_status': 'SUBMITTED',
                    'order_id': 'FUTU-2',
                    'qty': kwargs['qty'],
                    'price': kwargs['price'],
                }
            ]
        )

    def modify_order(self, **kwargs):
        self.modify_calls.append(kwargs)
        return 0, pd.DataFrame()

    def close(self):
        self.closed = True


def _broker(fake=None):
    fake = fake or FakeTradeContext()
    context = SimpleNamespace(
        futu_trade_context=fake,
        # 测试数据同时覆盖美元证券；显式声明账户计价币种，避免把测试替身
        # 缺失的 FX 行情误当成真实换汇成功。
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    )
    broker = FutuBrokerAdapter(context)
    return broker, fake


def test_futu_config_exposes_broker_environment_and_alias():
    assert 'futu_broker' in config.BROKER_ENVIRONMENTS
    assert config.BROKER_ENVIRONMENTS['futu_broker']['sim']['trd_env'] == 'SIMULATE'
    assert config.BROKER_ENVIRONMENTS['futu_broker']['sim_event']['trigger'] == 'subscription'
    assert config.BROKER_ENVIRONMENTS['futu_broker']['real_event']['event_subtype'] == 'K_DAY'

    from data_providers.manager import normalize_source_name, resolve_platform_default_source

    assert resolve_platform_default_source('futu_broker') == 'futu'
    assert normalize_source_name('futu_broker') == 'futu'


def test_futu_defaults_to_simulated_trade_environment():
    """未显式选择实盘环境时，适配器必须使用模拟盘。"""
    import live_trader.adapters.futu_broker as futu_module

    broker, _ = _broker()

    assert broker._trade_env == 'SIMULATE'
    assert broker._trade_env_value() == getattr(futu_module.TrdEnv, 'SIMULATE', 'SIMULATE')


def test_futu_quote_context_waits_for_ready_before_event_subscription(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module

    class DelayedQuote:
        def __init__(self, **_kwargs):
            self.status_reads = 0
            self.closed = False

        @property
        def status(self):
            self.status_reads += 1
            return 'READY' if self.status_reads >= 2 else 'START'

        def close(self):
            self.closed = True

    monkeypatch.setattr(futu_module, 'OpenQuoteContext', DelayedQuote)
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')
    broker, _ = _broker()

    quote_context = broker._get_quote_context()

    assert quote_context is not None
    assert quote_context.status_reads >= 2
    assert broker._quote_context_init_failed is False


def test_futu_mixed_assets_keep_position_and_order_sizing_symbol_scoped():
    """股票、期权和其他衍生品共存时，仓位与目标订单必须按标的隔离。"""
    class MixedAssets(FakeTradeContext):
        def __init__(self):
            super().__init__()
            self.position_queries = []
            self.orders = pd.DataFrame([
                {
                    'code': 'US.AAPL',
                    'order_market': 'US',
                    'trd_side': 'BUY',
                    'order_status': 'SUBMITTED',
                    'order_id': 'STOCK-PENDING',
                    'qty': 4,
                    'dealt_qty': 1,
                },
                {
                    'code': 'US.AAPL260918P320000',
                    'order_market': 'US',
                    'trd_side': 'BUY',
                    'order_status': 'SUBMITTED',
                    'order_id': 'OPTION-PENDING',
                    'qty': 3,
                    'dealt_qty': 1,
                },
            ])

        def position_list_query(self, **kwargs):
            self.position_queries.append(kwargs)
            return 0, pd.DataFrame([
                {
                    'code': 'US.AAPL',
                    'position_market': 'US',
                    'qty': 10,
                    'can_sell_qty': 10,
                    'average_cost': 100.0,
                },
                {
                    'code': 'US.AAPL260918P320000',
                    'position_market': 'US',
                    'qty': 2,
                    'can_sell_qty': 2,
                    'average_cost': 10.0,
                },
                {
                    'code': 'US.NQ260918',
                    'position_market': 'US',
                    'qty': 3,
                    'can_sell_qty': 3,
                    'average_cost': 20.0,
                },
            ])

    class MixedQuote:
        def get_market_snapshot(self, codes):
            rows = {
                'US.AAPL': {'code': 'US.AAPL', 'last_price': 100.0},
                'US.AAPL260918P320000': {
                    'code': 'US.AAPL260918P320000',
                    'last_price': 10.0,
                    'option_contract_multiplier': 100.0,
                },
                'US.NQ260918': {
                    'code': 'US.NQ260918',
                    'last_price': 20.0,
                    'contract_multiplier': 10.0,
                },
            }
            return 0, pd.DataFrame([rows[code] for code in codes if code in rows])

    stock = SimpleNamespace(_name='US.AAPL', close=[100.0])
    option = SimpleNamespace(_name='US.AAPL260918P320000', close=[10.0])
    future = SimpleNamespace(_name='US.NQ260918', close=[20.0])
    trade = MixedAssets()
    broker, _ = _broker(trade)
    broker._quote_ctx = MixedQuote()
    broker.set_datas([stock, option, future])

    assert broker.get_position(stock).size == 10
    assert broker.get_position(option).size == 2
    assert broker.get_position(future).size == 3
    assert broker.get_current_price(future) == 20.0
    assert broker.get_contract_multiplier(future) == 10.0
    assert broker.get_position_market_value(future, 3, price=20.0) == 600.0
    assert all(query['code'] in {'US.AAPL', 'US.AAPL260918P320000', 'US.NQ260918'}
               for query in trade.position_queries)
    assert broker.get_cash() == 10000.0
    assert broker._get_portfolio_nav() == 12000.0
    assert broker.get_expected_size(stock) == 13
    assert broker.get_expected_size(option) == 4
    assert broker.get_expected_size(future) == 3

    option_order = broker.order_target_value(option, target=5000.0)

    assert option_order is not None
    assert trade.place_calls[-1]['code'] == 'US.AAPL260918P320000'
    assert trade.place_calls[-1]['qty'] == 1.0
    assert broker.get_expected_size(stock) == 13
    assert broker.get_expected_size(future) == 3


def test_futu_position_query_does_not_let_unrelated_short_position_block_stock():
    """整账户仓位返回时，无关 unsupported short 不应阻塞股票查询。"""
    class MixedWithShort(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame([
                {
                    'code': 'US.AAPL',
                    'position_market': 'US',
                    'qty': 10,
                    'can_sell_qty': 10,
                    'average_cost': 100.0,
                },
                {
                    'code': 'US.NQ260918',
                    'position_market': 'US',
                    'position_side': 'SHORT',
                    'qty': 1,
                    'can_sell_qty': 1,
                    'average_cost': 20.0,
                },
            ])

    broker, _ = _broker(MixedWithShort())
    stock = SimpleNamespace(_name='US.AAPL')
    future = SimpleNamespace(_name='US.NQ260918')

    assert broker.get_position(stock).size == 10
    with pytest.raises(RuntimeError, match='short position is unsupported'):
        broker.get_position(future)


def test_futu_missing_sdk_error_points_to_requirements_install(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module

    monkeypatch.setattr(futu_module, 'OpenSecTradeContext', None)
    with pytest.raises(ImportError, match=r'pip install -r requirements\.txt'):
        futu_module._require_futu_trade_sdk()


def test_futu_event_subscription_resolves_timeframe_to_kline_subtype():
    import live_trader.adapters.futu_broker as futu_module

    assert futu_module._futu_event_subtype_name(None, 'Days', 1) == 'K_DAY'
    assert futu_module._futu_event_subtype_name(None, 'Minutes', 5) == 'K_5M'
    assert futu_module._futu_event_subtype_name('QUOTE', 'Days', 1) == 'QUOTE'

    with pytest.raises(ValueError, match='no second-level'):
        futu_module._futu_event_subtype_name(None, 'Seconds', 1)


def test_futu_event_mode_rejects_combined_schedule_configuration():
    import live_trader.adapters.futu_broker as futu_module

    with pytest.raises(ValueError, match='cannot be combined with schedule'):
        futu_module.FutuBrokerAdapter.launch(
            {
                'trigger': 'subscription',
                'schedule': '1d:14:55:00',
            },
            'sample_strategy',
            {},
        )


def test_futu_event_timestamp_uses_latest_row_and_configured_timezone():
    import pytz
    import live_trader.adapters.futu_broker as futu_module

    content = pd.DataFrame({
        'code': ['SH.600000', 'SH.600000'],
        'time_key': ['2026-09-01 14:54:00', '2026-09-01 14:55:00'],
    })

    timestamp = futu_module._futu_event_timestamp(content, pytz.timezone('Asia/Shanghai'))

    assert timestamp.isoformat() == '2026-09-01T14:55:00+08:00'


def test_futu_quote_event_handler_forwards_parsed_content_to_callback():
    import live_trader.adapters.futu_broker as futu_module

    received = []

    class HandlerBase:
        def __init__(self):
            return None

        def on_recv_rsp(self, payload):
            return 0, payload

    handler = futu_module._create_futu_quote_event_handler(
        HandlerBase,
        received.append,
    )
    payload = pd.DataFrame([{'code': 'SH.600000', 'time_key': '2026-09-01 14:55:00'}])

    result = handler.on_recv_rsp(payload)

    assert result == (0, payload)
    assert received == [payload]


def test_futu_launch_connection_settings_are_shared_with_provider(monkeypatch):
    monkeypatch.setattr(config, 'FUTU_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'FUTU_PORT', 11111)
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')

    FutuBrokerAdapter._apply_connection_settings(
        '192.0.2.10',
        22222,
        r'C:\Users\Lin\.futursa',
    )

    assert config.FUTU_HOST == '192.0.2.10'
    assert config.FUTU_PORT == 22222
    assert config.FUTU_RSA_KEY_PATH == r'C:\Users\Lin\.futursa'


def test_futu_order_proxy_maps_status_and_execution_fields():
    proxy = FutuOrderProxy(
        {
            'order_id': 'FUTU-1',
            'trd_side': 'BUY',
            'order_status': 'FILLED_PART',
            'qty': 2.5,
            'dealt_qty': 1.25,
            'dealt_avg_price': 4.0,
            'commission': 0.2,
            'updated_time': '2026-08-31 10:20:30',
        }
    )
    assert proxy.id == 'FUTU-1'
    assert proxy.is_buy()
    assert proxy.is_pending()
    assert proxy.is_accepted()
    assert not proxy.is_completed()
    assert proxy.executed.size == 1.25
    assert proxy.executed.price == 4.0
    assert proxy.executed.value == 5.0
    assert proxy.executed.comm == 0.2
    assert proxy.executed.dt.isoformat() == '2026-08-31T10:20:30'

    canceled = FutuOrderProxy({'order_id': 'FUTU-3', 'trd_side': 'SELL', 'order_status': 'CANCELLED_ALL'})
    assert canceled.is_sell()
    assert canceled.is_canceled()
    assert not canceled.is_pending()
    assert not canceled.is_accepted()


def test_futu_option_order_proxy_value_uses_contract_multiplier():
    proxy = FutuOrderProxy(
        {
            'order_id': 'FUTU-OPTION-1',
            'trd_side': 'BUY',
            'order_status': 'FILLED_ALL',
            'dealt_qty': 1,
            'dealt_avg_price': 10,
        },
        contract_multiplier=100,
    )

    assert proxy.executed.value == 1000


def test_futu_order_proxy_falls_back_to_raw_option_metadata_when_explicit_multiplier_invalid():
    proxy = FutuOrderProxy(
        {
            'order_id': 'FUTU-OPTION-INVALID-MULT',
            'trd_side': 'BUY',
            'order_status': 'FILLED_ALL',
            'dealt_qty': 1,
            'dealt_avg_price': 10,
            'option_contract_size': 100,
        },
        contract_multiplier=0,
    )

    assert proxy.executed.value == 1000


def test_futu_convert_order_proxy_preserves_multiplier_for_filled_option():
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL260918P320000')
    broker.set_datas([data])
    broker._contract_multipliers['US.AAPL260918P320000'] = 100.0

    proxy = broker.convert_order_proxy(
        {
            'order_id': 'FUTU-OPTION-2',
            'code': 'US.AAPL260918P320000',
            'trd_side': 'BUY',
            'order_status': 'FILLED_ALL',
            'dealt_qty': 1,
            'dealt_avg_price': 10,
        }
    )

    assert proxy.data is data
    assert proxy.executed.value == 1000


def test_futu_adapter_queries_cash_position_and_pending_orders():
    broker, fake = _broker()
    data = SimpleNamespace(_name='SHSE.600000', close=[10.5])
    broker.set_datas([data])

    assert fake.timeout == 5.0
    assert broker.get_cash() == 10000.0
    assert broker.getvalue() == 12000.0

    position = broker.get_position(data)
    assert position.size == 100
    assert position.price == 10.5
    assert position.sellable == 60
    assert broker.get_sellable_position(data) == 60

    pending = broker.get_pending_orders()
    assert pending == [{'id': 'FUTU-1', 'symbol': 'SHSE.600000', 'direction': 'BUY', 'size': 75}]
    assert broker._last_pending_orders_fetch_failed is False


def test_futu_cash_falls_back_when_preferred_account_fields_are_na():
    class CashOnly(FakeTradeContext):
        def accinfo_query(self, **kwargs):
            return 0, pd.DataFrame(
                [{'available_funds': 'N/A', 'net_cash_power': 'N/A', 'power': 0.0, 'cash': 999277.145}]
            )

    broker, _ = _broker(CashOnly())
    assert broker.get_cash() == 999277.145


def test_futu_cash_prefers_account_currency_specific_buying_power():
    class CurrencyCash(FakeTradeContext):
        def accinfo_query(self, **kwargs):
            return 0, pd.DataFrame([{
                'available_funds': 'N/A',
                'net_cash_power': 1.0,
                'usd_net_cash_power': 123.45,
                'us_cash': 120.0,
                'total_assets': 200.0,
            }])

    broker, _ = _broker(CurrencyCash())

    assert broker.get_cash() == pytest.approx(123.45)


def test_futu_adapter_submits_and_cancels_using_official_api():
    broker, fake = _broker()
    data = SimpleNamespace(_name='US.AAPL', close=[200.0])

    proxy = broker._submit_order(data, 3.5, 'BUY', 200.0)
    assert proxy is not None
    assert proxy.id == 'FUTU-2'
    assert proxy.submitted_size == 3.5
    assert fake.place_calls[0]['code'] == 'US.AAPL'
    assert fake.place_calls[0]['qty'] == 3.5
    assert fake.place_calls[0]['trd_side'] == 'BUY'
    assert fake.place_calls[0]['order_type'] == config.FUTU_ORDER_TYPE

    assert broker.cancel_pending_order('FUTU-1') is True
    assert fake.modify_calls[0]['order_id'] == 'FUTU-1'
    assert fake.modify_calls[0]['modify_order_op'] == 'CANCEL'


def test_futu_connection_environment_is_not_overwritten_by_config_snapshot():
    fake = FakeTradeContext()
    context = SimpleNamespace(
        futu_trade_context=fake,
        _futu_runtime_config={
            'FUTU_TRADE_ENV': 'SIMULATE',
            'FUTU_ACCOUNT_CURRENCY': 'HKD',
        },
    )
    broker = FutuBrokerAdapter(context)
    data = SimpleNamespace(_name='HK.00700')
    broker._submit_order(data, 1, 'BUY', 100.0)
    assert fake.place_calls[0]['trd_env'] == 'SIMULATE'


def test_futu_pending_query_failure_is_fail_closed():
    class Broken(FakeTradeContext):
        def order_list_query(self, **kwargs):
            raise RuntimeError('OpenD pending unavailable')

    broker, _ = _broker(Broken())
    assert broker.get_pending_orders() == []
    assert broker._last_pending_orders_fetch_failed is True
    assert 'OpenD pending unavailable' in str(broker._last_pending_orders_fetch_error)
    assert broker.cancel_pending_order('FUTU-1') is False


def test_futu_a_share_position_without_sellable_field_is_not_sellable():
    class NoSellable(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame(
                [{'code': '600000', 'position_market': 'SH', 'qty': 100, 'average_cost': 10.0}]
            )

    broker, _ = _broker(NoSellable())
    data = SimpleNamespace(_name='SHSE.600000')
    assert broker.get_position(data).size == 100
    assert broker.get_sellable_position(data) == 0


def test_futu_position_cost_falls_back_when_average_cost_is_na():
    class CostFallback(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame([{
                'code': '600000',
                'position_market': 'SH',
                'qty': 100,
                'can_sell_qty': 100,
                'average_cost': 'N/A',
                'cost_price': 10.25,
            }])

    broker, _ = _broker(CostFallback())
    position = broker.get_position(SimpleNamespace(_name='SHSE.600000'))

    assert position.price == pytest.approx(10.25)


def test_futu_current_price_uses_injected_quote_context_and_fails_closed():
    broker, _ = _broker()

    class Quote:
        def get_market_snapshot(self, codes):
            return 0, pd.DataFrame([{'code': 'US.AAPL', 'last_price': 201.5}])

    broker._quote_ctx = Quote()
    assert broker.get_current_price(SimpleNamespace(_name='AAPL', close=[200.0])) == 201.5

    class BrokenQuote:
        def get_market_snapshot(self, codes):
            raise RuntimeError('quote unavailable')

    broker._quote_ctx = BrokenQuote()
    assert broker.get_current_price(SimpleNamespace(_name='AAPL', close=[200.0])) == 0.0


def test_futu_option_multiplier_can_be_discovered_without_injected_quote_context(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module

    class OwnedQuote:
        def __init__(self, **kwargs):
            self.closed = False

        def set_sync_query_connect_timeout(self, timeout):
            self.timeout = timeout

        def get_market_snapshot(self, codes):
            return 0, pd.DataFrame(
                [{
                    'code': 'US.AAPL260918P320000',
                    'last_price': 10.0,
                    'option_contract_multiplier': 100.0,
                }]
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(futu_module, 'OpenQuoteContext', OwnedQuote)
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL260918P320000', close=[10.0])

    assert broker.get_current_price(data) == 10.0
    assert broker.get_contract_multiplier(data) == 100.0
    owned_quote = broker._quote_ctx
    broker.close()
    assert owned_quote.closed is True


def test_futu_option_contract_size_is_used_when_contract_multiplier_is_zero():
    broker, _ = _broker()

    class Quote:
        def get_market_snapshot(self, codes):
            return 0, pd.DataFrame(
                [{
                    'code': codes[0],
                    'last_price': 10.0,
                    'option_contract_multiplier': 0.0,
                    'option_contract_size': 100.0,
                }]
            )

    broker._quote_ctx = Quote()
    data = SimpleNamespace(_name='US.AAPL260918P320000', close=[10.0])

    assert broker.get_current_price(data) == 10.0
    assert broker.get_contract_multiplier(data) == 100.0


def test_futu_option_specific_multiplier_precedes_generic_default():
    broker, _ = _broker()
    data = SimpleNamespace(
        _name='US.AAPL260918P320000',
        p=SimpleNamespace(dataname=pd.DataFrame({
            'contract_multiplier': [1.0],
            'option_contract_multiplier': [100.0],
        })),
    )

    assert broker.get_contract_multiplier(data) == 100.0


def test_futu_rejects_short_and_combination_positions():
    class ShortPosition(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame([{
                'code': 'US.AAPL',
                'position_market': 'US',
                'position_side': 'SHORT',
                'qty': 1,
                'can_sell_qty': 1,
                'average_cost': 100.0,
            }])

    broker, _ = _broker(ShortPosition())
    with pytest.raises(RuntimeError, match='short position is unsupported'):
        broker.get_position(SimpleNamespace(_name='US.AAPL'))

    class ComboPosition(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame([{
                'code': 'US.AAPL',
                'position_market': 'US',
                'position_type': 'COMBINED',
                'combo_id': 'COMBO-1',
                'qty': 1,
                'can_sell_qty': 1,
                'average_cost': 100.0,
            }])

    broker, _ = _broker(ComboPosition())
    with pytest.raises(RuntimeError, match='combination position is unsupported'):
        broker.get_position(SimpleNamespace(_name='US.AAPL'))


def test_futu_a_share_buy_enforces_hundred_share_lot():
    broker, fake = _broker()
    data = SimpleNamespace(_name='SHSE.600000', close=[10.0])

    assert broker.get_order_lot_size(data) == 100.0
    assert broker._submit_order(data, 37, 'BUY', 10.0) is None
    assert broker._last_order_target_skip_reason == 'a_share_buy_lot_misaligned'
    assert fake.place_calls == []


def test_futu_retries_quote_context_after_transient_initialization_failure(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module

    created = []

    class Quote:
        def __init__(self, **kwargs):
            created.append(self)

        def get_market_snapshot(self, codes):
            return 0, pd.DataFrame([{'code': codes[0], 'last_price': 10.0}])

        def close(self):
            return None

    calls = {'count': 0}

    def open_quote(**kwargs):
        calls['count'] += 1
        if calls['count'] == 1:
            raise RuntimeError('temporary OpenD failure')
        return Quote(**kwargs)

    monkeypatch.setattr(futu_module, 'OpenQuoteContext', open_quote)
    broker, _ = _broker()
    broker._quote_context_retry_at = 0.0
    assert broker._get_quote_context() is None
    broker._quote_context_retry_at = 0.0
    assert broker._get_quote_context() is created[0]
    assert broker._quote_context_init_failed is False


def test_futu_rebuilds_closed_trade_context_after_disconnect(monkeypatch):
    """交易上下文断线进入 CLOSED 后不得继续复用旧句柄。"""
    import live_trader.adapters.futu_broker as futu_module

    class ClosedTrade:
        status = 'CLOSED'

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FreshTrade(FakeTradeContext):
        status = 'READY'

    stale = ClosedTrade()
    created = []

    def open_trade(**_kwargs):
        fresh = FreshTrade()
        created.append(fresh)
        return fresh

    monkeypatch.setattr(futu_module, 'OpenSecTradeContext', open_trade)
    broker, _ = _broker()
    broker._trade_ctx = stale
    broker.trade_ctx = stale
    broker.trd_ctx = stale
    broker._owns_trade_ctx = True

    fresh = broker._get_trade_context()

    assert stale.closed is True
    assert created == [fresh]
    assert broker._trade_ctx is fresh
    assert broker.trade_ctx is fresh
    assert broker.trd_ctx is fresh
    assert broker._trade_context_init_failed is False


def test_futu_quote_unavailable_never_falls_back_to_stale_bar(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module

    monkeypatch.setattr(futu_module, 'OpenQuoteContext', None)
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL', close=[200.0])

    assert broker.get_current_price(data) == 0.0


def test_futu_uses_account_currency_for_nav_and_order_unit_value():
    class Quote:
        def get_market_snapshot(self, codes):
            code = codes[0]
            if code == 'US.AAPL':
                return 0, pd.DataFrame([{'code': code, 'last_price': 200.0}])
            if code == 'FX.USDHKD':
                return 0, pd.DataFrame([{'code': code, 'last_price': 7.8}])
            return 0, pd.DataFrame()

    broker, _ = _broker()
    broker._context._futu_runtime_config['FUTU_ACCOUNT_CURRENCY'] = 'HKD'
    broker._quote_ctx = Quote()
    data = SimpleNamespace(_name='US.AAPL', close=[200.0])

    assert broker._order_unit_value(data, 200.0) == pytest.approx(1560.0)
    assert broker.get_position_market_value(data, 2, price=200.0) == pytest.approx(3120.0)


def test_futu_account_currency_normalizes_cny_to_supported_cnh_enum():
    broker, _ = _broker()
    broker._context._futu_runtime_config['FUTU_ACCOUNT_CURRENCY'] = 'CNY'

    assert broker._account_currency_name() == 'CNH'


def test_futu_fx_conversion_can_triangulate_through_usd():
    class Quote:
        def get_market_snapshot(self, codes):
            code = codes[0]
            values = {
                'FX.USDCNH': 7.2,
                'FX.USDHKD': 7.8,
            }
            if code in values:
                return 0, pd.DataFrame([{'code': code, 'last_price': values[code]}])
            return 0, pd.DataFrame()

    broker, _ = _broker()
    broker._context._futu_runtime_config['FUTU_ACCOUNT_CURRENCY'] = 'HKD'
    broker._quote_ctx = Quote()

    assert broker._get_fx_rate('CNH', 'HKD') == pytest.approx(7.8 / 7.2)


def test_futu_position_valuation_fails_closed_when_fx_is_unavailable():
    broker, _ = _broker()
    broker._context._futu_runtime_config['FUTU_ACCOUNT_CURRENCY'] = 'USD'
    broker._quote_ctx = type('Quote', (), {
        'get_market_snapshot': lambda self, codes: (0, pd.DataFrame()),
    })()
    data = SimpleNamespace(_name='HK.00700')

    with pytest.raises(RuntimeError, match='position valuation unavailable'):
        broker.get_position_market_value(data, 1, price=300.0)


def test_futu_schedule_filter_rejects_weekends_and_closed_market():
    broker, _ = _broker()

    class Quote:
        def get_market_state(self, codes):
            return 0, pd.DataFrame([
                {'code': codes[0], 'market_state': 'CLOSED'},
            ])

    broker._quote_ctx = Quote()
    broker.set_datas([SimpleNamespace(_name='HK.00700')])

    assert broker.is_trading_slot(pd.Timestamp('2026-09-06 15:00:00')) is False
    assert broker.is_trading_slot(pd.Timestamp('2026-09-01 15:00:00')) is False

    broker._quote_ctx = type('OpenQuote', (), {
        'get_market_state': lambda self, codes: (
            0, pd.DataFrame([{'code': codes[0], 'market_state': 'AFTERNOON'}])
        )
    })()
    assert broker.is_trading_slot(pd.Timestamp('2026-09-01 15:00:00')) is True


def test_futu_option_multiplier_reads_dataframe_contract_size_when_multiplier_is_zero():
    broker, _ = _broker()
    data = SimpleNamespace(
        _name='US.AAPL260918P320000',
        p=SimpleNamespace(dataname=pd.DataFrame({
            'option_contract_multiplier': [0.0],
            'option_contract_size': [100.0],
        })),
    )

    assert broker.get_contract_multiplier(data) == 100.0


def test_futu_option_multiplier_reads_dataframe_attrs():
    broker, _ = _broker()
    dataframe = pd.DataFrame({'close': [10.0]})
    dataframe.attrs['option_contract_multiplier'] = 100.0
    data = SimpleNamespace(
        _name='US.AAPL260918P320000',
        p=SimpleNamespace(dataname=dataframe),
    )

    assert broker.get_contract_multiplier(data) == 100.0


def test_futu_option_metadata_wins_over_datafeed_generic_default_multiplier():
    broker, _ = _broker()
    dataframe = pd.DataFrame({'option_contract_size': [100.0]})
    data = SimpleNamespace(
        _name='US.AAPL260918P320000',
        contract_multiplier=1.0,
        p=SimpleNamespace(dataname=dataframe),
    )

    assert broker.get_contract_multiplier(data) == 100.0


def test_futu_option_without_multiplier_fails_closed_in_order_value():
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL260918P320000')

    assert broker.get_contract_multiplier(data) == 0.0
    assert broker._order_unit_value(data, 10.0) == 0.0


def test_futu_option_quote_failure_fails_closed_instead_of_using_stale_bar(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module

    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')
    monkeypatch.setattr(futu_module, 'OpenQuoteContext', None)
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL260918P320000', close=[10.0])

    assert broker.get_current_price(data) == 0.0


def test_futu_option_contract_multiplier_scales_target_order_and_reserved_cash():
    class EmptyPositions(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame()

    class OptionQuote:
        def get_market_snapshot(self, codes):
            return 0, pd.DataFrame(
                [{
                    'code': 'US.AAPL260918P320000',
                    'last_price': 10.0,
                    'option_contract_multiplier': 100.0,
                }]
            )

    broker, fake = _broker(EmptyPositions())
    broker._quote_ctx = OptionQuote()
    data = SimpleNamespace(_name='US.AAPL260918P320000', close=[10.0])
    broker.set_datas([data])

    proxy = broker.order_target_percent(data, 0.10)

    assert proxy is not None
    assert fake.place_calls[0]['qty'] == 1.0
    assert fake.place_calls[0]['price'] == 10.0
    assert proxy.reserved_cash == 1001.3


def test_futu_option_contract_multiplier_is_included_in_portfolio_nav():
    class OneOptionPosition(FakeTradeContext):
        def position_list_query(self, **kwargs):
            return 0, pd.DataFrame(
                [{
                    'code': 'US.AAPL260918P320000',
                    'position_market': 'US',
                    'qty': 1,
                    'can_sell_qty': 1,
                    'average_cost': 10.0,
                }]
            )

    class OptionQuote:
        def get_market_snapshot(self, codes):
            return 0, pd.DataFrame(
                [{
                    'code': 'US.AAPL260918P320000',
                    'last_price': 10.0,
                    'option_contract_multiplier': 100.0,
                }]
            )

    broker, _ = _broker(OneOptionPosition())
    broker._quote_ctx = OptionQuote()
    data = SimpleNamespace(_name='US.AAPL260918P320000', close=[10.0])
    broker.set_datas([data])

    assert broker._get_portfolio_nav() == 12000.0


def test_futu_nav_does_not_fallback_to_partial_local_valuation_on_account_failure():
    class BrokenAccount(FakeTradeContext):
        def accinfo_query(self, **kwargs):
            raise RuntimeError('account snapshot unavailable')

    broker, _ = _broker()
    broker._trade_ctx = BrokenAccount()
    broker.trade_ctx = broker._trade_ctx
    broker.trd_ctx = broker._trade_ctx
    with pytest.raises(RuntimeError, match='account snapshot unavailable'):
        broker._get_portfolio_nav()


def test_futu_launch_delegates_schedule_lifecycle_to_common_runner(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module
    import live_trader.engine as engine_module

    captured = {}

    class DummyTradeContext:
        def set_handler(self, handler):
            captured['handler'] = handler

        def start(self):
            captured['started'] = True

    class DummyBroker:
        _trade_ctx = DummyTradeContext()
        datas = []

        def close(self):
            captured['closed'] = True

    class DummyTrader:
        def __init__(self, config):
            captured['engine_config'] = config
            self.broker = DummyBroker()
            self.data_provider = None

        def init(self, context):
            captured['context'] = context

    class DummyRunner:
        def __init__(self, **kwargs):
            captured['runner_kwargs'] = kwargs

        def run_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(engine_module, 'LiveTrader', DummyTrader)
    monkeypatch.setattr(futu_module, 'LiveScheduleRunner', DummyRunner)
    monkeypatch.setattr(config, 'FUTU_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'FUTU_PORT', 11111)
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')

    FutuBrokerAdapter.launch(
        {
            'schedule': '1h:10:00:00',
            'timezone': 'Asia/Shanghai',
            'trd_env': 'SIMULATE',
        },
        'sample_auto_rebalance_strategy',
        {},
        symbols=['HK.00700'],
    )

    runner_kwargs = captured['runner_kwargs']
    assert runner_kwargs['schedule_rule'] == '1h:10:00:00'
    assert runner_kwargs['parsed_schedule']['interval_seconds'] == 3600.0
    assert callable(runner_kwargs['on_slot'])
    assert callable(runner_kwargs['on_prewarm'])
    assert captured['started'] is True
    assert captured['closed'] is True


def test_futu_launch_supports_quote_subscription_event_mode(monkeypatch):
    import live_trader.adapters.futu_broker as futu_module
    import live_trader.engine as engine_module

    captured = {}

    class DummyTradeContext:
        def set_handler(self, handler):
            captured['trade_handler'] = handler

        def start(self):
            captured['trade_started'] = True

    class DummyQuoteContext:
        def set_handler(self, handler):
            captured['quote_handler'] = handler
            return 0

        def subscribe(self, codes, subtypes, **kwargs):
            captured['subscription'] = (codes, subtypes, kwargs)
            return 0, None

        def start(self):
            captured['quote_started'] = True
            raise KeyboardInterrupt

    class DummyData:
        _name = 'SHSE.600519'

    quote_context = DummyQuoteContext()

    class DummyBroker:
        _trade_ctx = DummyTradeContext()
        datas = [DummyData()]

        def _get_quote_context(self):
            return quote_context

        def close(self):
            captured['closed'] = True

    class DummyProvider:
        def close(self):
            captured['provider_closed'] = True

    class DynamicProvider:
        """动态 API Provider 不应把不存在的 close 解释成远程请求。"""

        def __init__(self):
            self.lookups = []

        def __getattr__(self, name):
            self.lookups.append(name)
            return lambda: (_ for _ in ()).throw(AssertionError(f'unexpected provider API: {name}'))

    dynamic_provider = DynamicProvider()

    class DummyTrader:
        def __init__(self, config):
            self.broker = DummyBroker()
            self.data_provider = None
            self._data_manager = type('DataManager', (), {
                'providers': [DummyProvider(), dynamic_provider],
            })()

        def init(self, context):
            captured['context'] = context

    monkeypatch.setattr(engine_module, 'LiveTrader', DummyTrader)
    monkeypatch.setattr(config, 'FUTU_HOST', '127.0.0.1')
    monkeypatch.setattr(config, 'FUTU_PORT', 11111)
    monkeypatch.setattr(config, 'FUTU_RSA_KEY_PATH', '')

    FutuBrokerAdapter.launch(
        {
            'trigger': 'subscription',
            'event_subtype': 'K_DAY',
            'timezone': 'Asia/Shanghai',
            'trd_env': 'SIMULATE',
        },
        'sample_auto_rebalance_strategy',
        {},
        symbols=['SHSE.600519'],
    )

    codes, subtypes, options = captured['subscription']
    assert codes == ['SH.600519']
    assert subtypes == [getattr(futu_module.SubType, 'K_DAY', 'K_DAY')]
    assert options['is_first_push'] is False
    assert options['subscribe_push'] is True
    assert captured['quote_started'] is True
    assert captured['provider_closed'] is True
    assert dynamic_provider.lookups == []
    assert captured['closed'] is True
