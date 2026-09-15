from types import SimpleNamespace

import pandas as pd
import pytest

import live_trader.adapters.futu_broker as futu_module
from live_trader.adapters.futu_broker import FutuBrokerAdapter
from common.options.contracts import (
    InvalidOptionOrderEffect,
    apply_signed_position,
    signed_position_delta,
    validate_option_order_effect,
)
from common.options.risk import OptionRiskLeg


class _Trade:
    def __init__(self, positions=None):
        self.positions = positions if positions is not None else pd.DataFrame()
        self.orders = pd.DataFrame()
        self.place_calls = []

    def set_sync_query_connect_timeout(self, _timeout):
        pass

    def accinfo_query(self, **_kwargs):
        return 0, pd.DataFrame([{'available_funds': 10000, 'total_assets': 10000}])

    def position_list_query(self, **_kwargs):
        return 0, self.positions.copy()

    def order_list_query(self, **_kwargs):
        return 0, self.orders.copy()

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return 0, pd.DataFrame([{
            'code': kwargs['code'],
            'trd_side': kwargs['trd_side'],
            'order_status': 'SUBMITTED',
            'order_id': f'ORDER-{len(self.place_calls)}',
            'qty': kwargs['qty'],
            'price': kwargs['price'],
        }])


class _ComboLegStub:
    """不依赖可选 futu-api 包的组合腿测试替身。"""

    pass


def _enable_combo_api(monkeypatch):
    """让组合订单测试只验证 adapter 逻辑，不把 SDK 安装作为测试前提。"""

    monkeypatch.setattr(futu_module, 'ComboLeg', _ComboLegStub)


def _broker(positions=None):
    trade = _Trade(positions)
    context = SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    )
    broker = FutuBrokerAdapter(context)
    broker._contract_multipliers['US.AAPL260918P320000'] = 100.0
    return broker, trade


def test_signed_position_effects_are_deterministic():
    assert signed_position_delta('BUY_TO_OPEN', 2) == 2
    assert signed_position_delta('SELL_TO_CLOSE', 2) == -2
    assert signed_position_delta('SELL_TO_OPEN', 2) == -2
    assert signed_position_delta('BUY_TO_CLOSE', 2) == 2
    assert apply_signed_position(3, 'SELL_TO_CLOSE', 1) == 2
    assert apply_signed_position(-3, 'BUY_TO_CLOSE', 1) == -2


def test_invalid_effect_and_position_boundaries_fail_closed():
    with pytest.raises(InvalidOptionOrderEffect):
        validate_option_order_effect('SELL_TO_OPEN', 0, 1)
    with pytest.raises(InvalidOptionOrderEffect):
        validate_option_order_effect('SELL_TO_CLOSE', 0, 1)
    with pytest.raises(InvalidOptionOrderEffect):
        validate_option_order_effect('BUY_TO_CLOSE', 2, 1)


def test_futu_explicit_sell_to_close_preserves_effect_and_direction():
    option = 'US.AAPL260918P320000'
    broker, trade = _broker(pd.DataFrame([{
        'code': option,
        'position_market': 'US',
        'qty': 3,
        'can_sell_qty': 3,
        'average_cost': 10.0,
    }]))
    data = SimpleNamespace(_name=option)

    proxy = broker.submit_option_order(data, 1, 'SELL_TO_CLOSE', price=11.0)

    assert proxy is not None
    assert proxy.order_effect == 'SELL_TO_CLOSE'
    assert trade.place_calls[0]['trd_side'] == 'SELL'


def test_futu_short_option_can_only_use_buy_to_close_and_sell_to_open_is_blocked():
    option = 'US.AAPL260918P320000'
    broker, trade = _broker(pd.DataFrame([{
        'code': option,
        'position_market': 'US',
        'position_side': 'SHORT',
        'qty': 2,
        'average_cost': 10.0,
    }]))
    data = SimpleNamespace(_name=option)

    close_proxy = broker.submit_option_order(data, 1, 'BUY_TO_CLOSE', price=9.0)
    rejected = broker.submit_option_order(data, 1, 'SELL_TO_OPEN', price=9.0)

    assert broker.get_position(data).size == -2
    assert close_proxy is not None
    assert close_proxy.order_effect == 'BUY_TO_CLOSE'
    assert trade.place_calls[0]['trd_side'] == 'BUY'
    assert rejected is None
    assert len(trade.place_calls) == 1


def test_futu_short_option_negative_qty_is_signed_short():
    option = 'US.AAPL260918P320000'
    broker, _ = _broker(pd.DataFrame([{
        'code': option,
        'position_market': 'US',
        'qty': -2,
        'can_sell_qty': -2,
        'average_cost': 10.0,
    }]))
    data = SimpleNamespace(_name=option)

    assert broker.get_position(data).size == -2


def test_futu_long_side_negative_qty_fails_closed():
    option = 'US.AAPL260918P320000'
    broker, _ = _broker(pd.DataFrame([{
        'code': option,
        'position_market': 'US',
        'position_side': 'LONG',
        'qty': -1,
        'average_cost': 10.0,
    }]))
    data = SimpleNamespace(_name=option)

    with pytest.raises(RuntimeError, match='conflicting long side'):
        broker.get_position(data)


def test_futu_broker_margin_snapshot_uses_real_cash_and_managed_underlying():
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL')
    broker.set_datas([data])
    leg = OptionRiskLeg(
        'US.AAPL260918P050000', 'US.AAPL', 'PUT', -1, 50, 5, 300, 100
    )
    snapshot = broker.get_option_margin_snapshot(
        [leg], underlying_positions={'US.AAPL': 0}
    )
    assert snapshot.margin_used == 5000
    assert snapshot.available_margin == 5000


def test_futu_sell_to_open_requires_explicit_secured_risk_leg():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    broker._contract_multipliers[option] = 100.0
    data = SimpleNamespace(_name=option)
    leg = OptionRiskLeg(option, 'US.AAPL', 'PUT', -1, 50, 5, 300, 100)

    assert broker.submit_option_order(data, 1, 'SELL_TO_OPEN', price=5) is None
    proxy = broker.submit_option_order(
        data,
        1,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=leg,
        underlying_positions={'US.AAPL': 0},
    )

    assert proxy is not None
    assert proxy.order_effect == 'SELL_TO_OPEN'
    assert trade.place_calls[-1]['trd_side'] == 'SELL'


def test_futu_sell_to_open_rejects_positive_risk_leg():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    data = SimpleNamespace(_name=option)
    long_leg = OptionRiskLeg(option, 'US.AAPL', 'PUT', 1, 50, 5, 300, 100)

    rejected = broker.submit_option_order(
        data,
        1,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=long_leg,
        underlying_positions={'US.AAPL': 0},
    )

    assert rejected is None
    assert trade.place_calls == []


def test_futu_single_leg_short_put_accepts_short_option_type_alias_and_reserves_cash():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    broker._contract_multipliers[option] = 100.0
    data = SimpleNamespace(_name=option)
    leg = OptionRiskLeg(option, 'US.AAPL', 'P', -1, 50, 5, 300, 100)

    proxy = broker.submit_option_order(
        data, 1, 'SELL_TO_OPEN', price=5,
        allow_sell_to_open=True, risk_leg=leg,
        underlying_positions={'US.AAPL': 0},
    )

    assert proxy is not None
    assert str(proxy.id) in broker._option_run_reservations
    assert trade.place_calls[-1]['trd_side'] == 'SELL'


def test_futu_spread_fails_closed_when_combo_api_is_unavailable():
    option = 'US.AAPL260918P320000'
    broker, trade = _broker()
    data = SimpleNamespace(_name=option)
    risk_leg = OptionRiskLeg(option, 'US.AAPL', 'PUT', -1, 320, 5, 300, 100)

    result = broker.submit_option_spread([
        {
            'data': data,
            'volume': 1,
            'effect': 'SELL_TO_OPEN',
            'price': 5,
            'risk_leg': risk_leg,
        },
        {
            'data': SimpleNamespace(_name='US.AAPL260918P300000'),
            'volume': 1,
            'effect': 'BUY_TO_OPEN',
            'price': 1,
            'risk_leg': OptionRiskLeg(
                'US.AAPL260918P300000', 'US.AAPL', 'PUT', 1, 300, 1, 300, 100,
            ),
        },
    ])

    assert result is None
    assert broker._last_order_target_skip_reason == 'atomic_option_spread_unsupported'
    assert trade.place_calls == []


def test_futu_spread_uses_combo_api_as_one_broker_order(monkeypatch):
    _enable_combo_api(monkeypatch)
    short_symbol = 'US.AAPL260918P320000'
    long_symbol = 'US.AAPL260918P300000'

    class ComboTrade(_Trade):
        def __init__(self):
            super().__init__(pd.DataFrame())
            self.combo_info_calls = []
            self.combo_place_calls = []

        def comboorder_tradinginfo_query(self, *args, **kwargs):
            self.combo_info_calls.append((args, kwargs))
            return 0, pd.DataFrame([{'initial_margin_change': 1000}])

        def place_combo_order(self, *args, **kwargs):
            self.combo_place_calls.append((args, kwargs))
            return 0, pd.DataFrame([{
                'order_id': 'COMBO-1',
                'code': short_symbol,
                'trd_side': 'SELL',
                'order_status': 'SUBMITTED',
                'qty': kwargs['qty'],
                'price': kwargs['price'],
            }])

    trade = ComboTrade()
    context = SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    )
    broker = FutuBrokerAdapter(context)
    broker._contract_multipliers[short_symbol] = 100.0
    broker._contract_multipliers[long_symbol] = 100.0
    short_data = SimpleNamespace(_name=short_symbol)
    long_data = SimpleNamespace(_name=long_symbol)
    result = broker.submit_option_spread([
        {
            'data': short_data,
            'volume': 1,
            'effect': 'SELL_TO_OPEN',
            'price': 5,
            'risk_leg': OptionRiskLeg(
                short_symbol, 'US.AAPL', 'PUT', -1, 320, 5, 300, 100,
                expiry='2026-09-18', price_shock=0.2,
            ),
        },
        {
            'data': long_data,
            'volume': 1,
            'effect': 'BUY_TO_OPEN',
            'price': 1,
            'risk_leg': OptionRiskLeg(
                long_symbol, 'US.AAPL', 'PUT', 1, 300, 1, 300, 100,
                expiry='2026-09-18', price_shock=0.2,
            ),
        },
    ])

    assert result is not None
    assert result.id == 'COMBO-1'
    assert result.is_combo is True
    assert len(trade.combo_info_calls) == 1
    assert len(trade.combo_place_calls) == 1
    assert trade.place_calls == []
    assert broker._option_run_reservations["COMBO-1"]["remaining"] == 1


def test_futu_close_spread_sends_integer_position_id(monkeypatch):
    _enable_combo_api(monkeypatch)
    short_symbol = 'US.AAPL260918P320000'
    long_symbol = 'US.AAPL260918P300000'

    class ComboTrade(_Trade):
        def __init__(self):
            super().__init__(pd.DataFrame([
                {
                    'code': short_symbol, 'position_market': 'US',
                    'position_side': 'SHORT', 'qty': 1,
                    'average_cost': 5.0, 'option_contract_multiplier': 100,
                    'position_id': '12345',
                },
                {
                    'code': long_symbol, 'position_market': 'US',
                    'qty': 1, 'can_sell_qty': 1, 'average_cost': 1.0,
                    'option_contract_multiplier': 100,
                    'position_id': '67890',
                },
            ]))
            self.combo_info_calls = []
            self.combo_place_calls = []

        def comboorder_tradinginfo_query(self, *args, **kwargs):
            self.combo_info_calls.append((args, kwargs))
            legs = args[0] if args else kwargs.get('combo_leg_list')
            for leg in legs:
                if getattr(leg, 'position_id', None) is None:
                    raise TypeError("'str' object cannot be interpreted as an integer")
                if not isinstance(leg.position_id, int):
                    raise TypeError("'str' object cannot be interpreted as an integer")
            return 0, pd.DataFrame([{'initial_margin_change': 0}])

        def place_combo_order(self, *args, **kwargs):
            self.combo_place_calls.append((args, kwargs))
            return 0, pd.DataFrame([{
                'order_id': 'COMBO-CLOSE-1',
                'code': short_symbol,
                'trd_side': 'BUY',
                'order_status': 'SUBMITTED',
                'qty': kwargs['qty'],
                'price': kwargs['price'],
            }])

    trade = ComboTrade()
    context = SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    )
    broker = FutuBrokerAdapter(context)
    broker._contract_multipliers[short_symbol] = 100.0
    broker._contract_multipliers[long_symbol] = 100.0
    result = broker.submit_option_spread([
        {'data': SimpleNamespace(_name=short_symbol), 'volume': 1,
         'effect': 'BUY_TO_CLOSE', 'price': 1.2},
        {'data': SimpleNamespace(_name=long_symbol), 'volume': 1,
         'effect': 'SELL_TO_CLOSE', 'price': 0.4},
    ])

    assert result is not None
    assert result.id == 'COMBO-CLOSE-1'
    legs = trade.combo_info_calls[0][0][0]
    assert sorted(leg.position_id for leg in legs) == [12345, 67890]


def test_futu_close_spread_rejects_mismatched_contract_multiplier(monkeypatch):
    _enable_combo_api(monkeypatch)
    short_symbol = 'US.AAPL260918P320000'
    hedge_symbol = 'US.AAPL260918P300000'

    class ComboTrade(_Trade):
        def __init__(self):
            super().__init__(pd.DataFrame([
                {
                    'code': short_symbol, 'position_market': 'US',
                    'position_side': 'SHORT', 'qty': 1,
                    'average_cost': 5.0, 'option_contract_multiplier': 100,
                },
                {
                    'code': hedge_symbol, 'position_market': 'US',
                    'qty': 1, 'can_sell_qty': 1, 'average_cost': 1.0,
                    'option_contract_multiplier': 50,
                },
            ]))
            self.combo_place_calls = []

        def comboorder_tradinginfo_query(self, *args, **kwargs):
            return 0, pd.DataFrame([{'initial_margin_change': 0}])

        def place_combo_order(self, *args, **kwargs):
            self.combo_place_calls.append((args, kwargs))
            return 0, pd.DataFrame([{'order_id': 'MUST-NOT-PLACE'}])

    trade = ComboTrade()
    context = SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    )
    broker = FutuBrokerAdapter(context)
    broker._contract_multipliers[short_symbol] = 100.0
    broker._contract_multipliers[hedge_symbol] = 50.0

    result = broker.submit_option_spread([
        {'data': SimpleNamespace(_name=short_symbol), 'volume': 1,
         'effect': 'BUY_TO_CLOSE', 'price': 1.0},
        {'data': SimpleNamespace(_name=hedge_symbol), 'volume': 1,
         'effect': 'SELL_TO_CLOSE', 'price': 1.0},
    ])

    assert result is None
    assert broker._last_order_target_skip_reason == 'option_spread_rejected'
    assert trade.combo_place_calls == []


def test_futu_spread_respects_option_entry_kill_switch(monkeypatch):
    _enable_combo_api(monkeypatch)
    short_symbol = 'US.AAPL260918P320000'
    long_symbol = 'US.AAPL260918P300000'

    class ComboTrade(_Trade):
        def comboorder_tradinginfo_query(self, *args, **kwargs):
            return 0, pd.DataFrame([{'initial_margin_change': 1000}])

        def place_combo_order(self, *args, **kwargs):
            self.combo_place_calls = getattr(self, 'combo_place_calls', [])
            self.combo_place_calls.append((args, kwargs))
            return 0, pd.DataFrame([{
                'order_id': 'COMBO-BLOCKED',
                'code': short_symbol,
                'trd_side': 'SELL',
                'order_status': 'SUBMITTED',
                'qty': kwargs['qty'],
                'price': kwargs['price'],
            }])

    trade = ComboTrade()
    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    ))
    broker.set_option_entry_kill_switch(True, source='watchdog', reason='gamma limit')
    short_data = SimpleNamespace(_name=short_symbol)
    long_data = SimpleNamespace(_name=long_symbol)
    result = broker.submit_option_spread([
        {'data': short_data, 'volume': 1, 'effect': 'SELL_TO_OPEN', 'price': 5,
         'risk_leg': OptionRiskLeg(short_symbol, 'US.AAPL', 'PUT', -1, 320, 5, 300, 100,
                                   expiry='2026-09-18', price_shock=0.2)},
        {'data': long_data, 'volume': 1, 'effect': 'BUY_TO_OPEN', 'price': 1,
         'risk_leg': OptionRiskLeg(long_symbol, 'US.AAPL', 'PUT', 1, 300, 1, 300, 100,
                                   expiry='2026-09-18', price_shock=0.2)},
    ])

    assert result is None
    assert broker._last_order_target_skip_reason == 'option_entry_kill_switch'
    assert getattr(trade, 'combo_place_calls', []) == []


def test_futu_pending_combo_without_leg_details_fails_closed():
    broker, _ = _broker()
    broker._query_order_rows = lambda: [{
        'order_id': 'COMBO-UNKNOWN',
        'code': 'US.AAPL260918P320000',
        'trd_side': 'SELL',
        'order_status': 'SUBMITTED',
        'order_type': 'COMBO',
        'qty': 1,
        'dealt_qty': 0,
    }]

    assert broker.get_pending_orders() == []
    assert broker._last_pending_orders_fetch_failed is True


def test_futu_sell_to_open_rejects_missing_data_multiplier():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    broker._contract_multipliers.clear()
    data = SimpleNamespace(_name=option)
    leg = OptionRiskLeg(option, 'US.AAPL', 'PUT', -1, 50, 5, 300, 100)

    rejected = broker.submit_option_order(
        data, 1, 'SELL_TO_OPEN', price=5,
        allow_sell_to_open=True, risk_leg=leg,
        underlying_positions={'US.AAPL': 0},
    )

    assert rejected is None
    assert broker._last_order_target_skip_reason == 'invalid_order_unit_value'
    assert trade.place_calls == []


def test_futu_option_buy_fails_closed_when_position_snapshot_is_unavailable():
    option = 'US.AAPL260918P320000'
    broker, trade = _broker()
    data = SimpleNamespace(_name=option)
    broker.get_position = lambda _data: (_ for _ in ()).throw(RuntimeError('position timeout'))

    rejected = broker._submit_order(data, 1, 'BUY', 5)

    assert rejected is None
    assert broker._last_order_target_skip_reason == 'unsupported_option_order_effect'
    assert trade.place_calls == []


def test_futu_single_leg_risk_metadata_must_match_contract():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    broker._contract_multipliers[option] = 100.0
    data = SimpleNamespace(_name=option)
    rejected = broker.submit_option_order(
        data,
        1,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=OptionRiskLeg(option, 'US.AAPL', 'PUT', -1, 1, 5, 300, 1),
        underlying_positions={'US.AAPL': 0},
    )

    assert rejected is None
    assert broker._last_order_target_skip_reason == 'unsupported_option_order_effect'
    assert trade.place_calls == []


def test_futu_combo_trading_info_without_margin_is_rejected():
    short_symbol = 'US.AAPL260918P320000'
    long_symbol = 'US.AAPL260918P300000'

    class ComboTrade(_Trade):
        def comboorder_tradinginfo_query(self, *args, **kwargs):
            return 0, pd.DataFrame([{'initial_margin_change': float('nan')}])

        def place_combo_order(self, *args, **kwargs):
            self.combo_place_calls = True
            return 0, pd.DataFrame([{'order_id': 'SHOULD-NOT-PLACE'}])

    trade = ComboTrade()
    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    ))
    result = broker.submit_option_spread([
        {'data': SimpleNamespace(_name=short_symbol), 'volume': 1, 'effect': 'SELL_TO_OPEN', 'price': 5,
         'risk_leg': OptionRiskLeg(short_symbol, 'US.AAPL', 'PUT', -1, 320, 5, 300, 100,
                                   expiry='2026-09-18', price_shock=0.2)},
        {'data': SimpleNamespace(_name=long_symbol), 'volume': 1, 'effect': 'BUY_TO_OPEN', 'price': 1,
         'risk_leg': OptionRiskLeg(long_symbol, 'US.AAPL', 'PUT', 1, 300, 1, 300, 100,
                                   expiry='2026-09-18', price_shock=0.2)},
    ])

    assert result is None
    assert getattr(trade, 'combo_place_calls', False) is False


def test_futu_combo_rejects_when_opening_margin_exceeds_available_cash():
    short_symbol = 'US.AAPL260918P320000'
    long_symbol = 'US.AAPL260918P300000'

    class ComboTrade(_Trade):
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{'cash': 500, 'available_funds': 500, 'total_assets': 10_000}])

        def comboorder_tradinginfo_query(self, *args, **kwargs):
            return 0, pd.DataFrame([{'initial_margin_change': 1000}])

        def place_combo_order(self, *args, **kwargs):
            self.combo_place_calls = True
            return 0, pd.DataFrame([{'order_id': 'SHOULD-NOT-PLACE'}])

    trade = ComboTrade()
    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    ))
    broker._contract_multipliers[short_symbol] = 100.0
    broker._contract_multipliers[long_symbol] = 100.0
    result = broker.submit_option_spread([
        {'data': SimpleNamespace(_name=short_symbol), 'volume': 1, 'effect': 'SELL_TO_OPEN', 'price': 5,
         'risk_leg': OptionRiskLeg(short_symbol, 'US.AAPL', 'PUT', -1, 320, 5, 300, 100,
                                   expiry='2026-09-18')},
        {'data': SimpleNamespace(_name=long_symbol), 'volume': 1, 'effect': 'BUY_TO_OPEN', 'price': 1,
         'risk_leg': OptionRiskLeg(long_symbol, 'US.AAPL', 'PUT', 1, 300, 1, 300, 100,
                                   expiry='2026-09-18')},
    ])

    assert result is None
    assert getattr(trade, 'combo_place_calls', False) is False


def test_futu_low_price_option_codes_parse_without_leading_zeroes():
    from live_trader.adapters.futu_broker import _parse_option_contract

    assert _parse_option_contract('US.MARA261016P9000') == {
        'expiry': '261016',
        'option_type': 'PUT',
        'strike': 9.0,
    }


def test_futu_csp_cash_is_rebuilt_from_short_positions_and_pending_orders():
    option = 'US.AAPL260918P050000'

    class CSPTrade(_Trade):
        def __init__(self):
            super().__init__(pd.DataFrame([{
                'code': option,
                'position_market': 'US',
                'position_side': 'SHORT',
                'qty': 1,
                'average_cost': 5.0,
                'option_contract_multiplier': 100,
            }]))
            self.orders = pd.DataFrame()

    broker, _ = _broker()
    broker._trade_ctx = CSPTrade()
    broker.trade_ctx = broker._trade_ctx
    broker.trd_ctx = broker._trade_ctx

    obligations = broker.get_option_assignment_obligations()
    assert obligations['assignment_cash'] == 5000
    assert broker.get_option_uncommitted_cash() == 5000
    assert broker.get_rebalance_cash() == 5000

    rejected = broker.submit_option_order(
        SimpleNamespace(_name=option),
        2,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=OptionRiskLeg(option, 'US.AAPL', 'PUT', -2, 50, 5, 300, 100),
        underlying_positions={'US.AAPL': 0},
    )
    assert rejected is None
    assert broker._last_order_target_skip_reason == 'unsupported_option_order_effect'


def test_futu_csp_cash_ignores_margin_buying_power_and_uses_cash_field():
    class PowerOnly(_Trade):
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                'usd_net_cash_power': 100_000,
                'available_funds': 'N/A',
                'cash': 5_000,
                'total_assets': 100_000,
            }])

    broker, _ = _broker()
    broker._trade_ctx = PowerOnly()
    broker.trade_ctx = broker._trade_ctx
    broker.trd_ctx = broker._trade_ctx

    assert broker.get_option_uncommitted_cash() == 5_000


def test_futu_csp_cash_uses_account_currency_cash_not_usd_bucket():
    class MixedCurrency(_Trade):
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                'usd_net_cash_power': 6.53,
                'us_cash': 6.32,
                'available_funds': 'N/A',
                'cash': 2683.61,
                'total_assets': 2648.33,
            }])

    broker, _ = _broker()
    broker._trade_ctx = MixedCurrency()
    broker.trade_ctx = broker._trade_ctx
    broker.trd_ctx = broker._trade_ctx

    assert float(broker.get_option_uncommitted_cash()) == pytest.approx(2683.61)


def test_futu_non_option_rebalance_keeps_normal_cash_semantics():
    class PowerOnly(_Trade):
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                'usd_net_cash_power': 100_000,
                'available_funds': 'N/A',
                'total_assets': 100_000,
            }])

    broker, _ = _broker()
    broker._trade_ctx = PowerOnly()
    broker.trade_ctx = broker._trade_ctx
    broker.trd_ctx = broker._trade_ctx

    assert broker.get_rebalance_cash() == 100_000


def test_futu_get_position_reuses_short_lived_all_position_snapshot(monkeypatch):
    option = "US.AAPL260918P320000"
    broker, trade = _broker(pd.DataFrame([{
        "code": option,
        "position_market": "US",
        "position_side": "SHORT",
        "qty": 2,
        "average_cost": 10.0,
    }]))
    calls = {"count": 0}
    original = trade.position_list_query

    def wrapped(**kwargs):
        calls["count"] += 1
        return original(**kwargs)

    trade.position_list_query = wrapped
    clock = {"now": 100.0}
    monkeypatch.setattr(futu_module.time, "monotonic", lambda: clock["now"])
    data = SimpleNamespace(_name=option)
    other = SimpleNamespace(_name="US.AAPL")
    assert broker.get_position(data).size == -2
    clock["now"] = 104.0
    assert broker.get_position(other).size == 0
    assert calls["count"] == 1
    clock["now"] = 100.0 + futu_module._POSITION_SNAPSHOT_CACHE_SECONDS + 0.1
    assert broker.get_position(data).size == -2
    assert calls["count"] == 2

def test_list_held_option_symbols_includes_short_put():
    option = "US.MARA261016P9000"
    broker, _trade = _broker(pd.DataFrame([{
        "code": option,
        "position_market": "US",
        "position_side": "SHORT",
        "qty": 1,
        "average_cost": 0.40,
        "option_contract_multiplier": 100,
    }]))
    assert option in broker.list_held_option_symbols()


def test_option_multiplier_uses_snapshot_when_not_in_datas():
    option = "US.MARA261016P9000"
    broker, _trade = _broker()
    broker.datas = []
    broker._contract_multipliers.clear()

    class Quote:
        def get_market_snapshot(self, symbols):
            assert symbols == [option]
            return 0, pd.DataFrame([{
                "code": option,
                "option_contract_multiplier": 100,
            }])

    broker._get_quote_context = lambda: Quote()
    assert broker._option_multiplier_for_symbol(option) == 100.0
    assert broker._contract_multipliers[option] == 100.0

