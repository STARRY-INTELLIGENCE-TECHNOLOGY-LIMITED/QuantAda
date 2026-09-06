import pandas as pd

from common.options.runtime import (
    compute_delta_hedge,
    refresh_option_chain,
    select_option_contract,
)


def _chain():
    return pd.DataFrame([
        {
            'option_symbol': 'US.AAPL261016P300000', 'option_type': 'PUT',
            'expiry': '2026-10-16', 'strike': 300, 'delta': -0.3,
            'bid': 2.0, 'ask': 2.2, 'last': 2.1,
        },
        {
            'option_symbol': 'US.AAPL261016P310000', 'option_type': 'PUT',
            'expiry': '2026-10-16', 'strike': 310, 'delta': -0.4,
            'bid': 2.0, 'ask': 2.05, 'last': 2.02,
        },
    ])


def test_chain_refresh_is_bounded_and_contract_selection_deterministic():
    class Provider:
        def get_option_chain_normalized(self, *_args, **_kwargs):
            return _chain()

    snapshot = refresh_option_chain(
        Provider(), 'US.AAPL', max_age_seconds=1,
        now='2026-09-01T00:00:00Z', monotonic=iter([0, 0.1]).__next__,
    )
    assert snapshot is not None
    row = select_option_contract(
        snapshot.data, option_type='PUT', max_delta=0.5,
        now='2026-09-01T00:00:00Z',
    )
    assert row['option_symbol'] == 'US.AAPL261016P310000'


def test_put_delta_filter_uses_signed_delta_and_target_distance():
    chain = _chain().assign(
        timestamp=pd.Timestamp('2026-09-01T00:00:00Z'),
        currency='USD',
    )
    row = select_option_contract(
        chain, option_type='PUT', min_delta=-0.45, max_delta=-0.25,
        target_delta=-0.30, now='2026-09-01T00:00:00Z',
    )
    assert row['option_symbol'] == 'US.AAPL261016P300000'


def test_chain_refresh_rejects_stale_or_caller_only_historical_timestamp():
    class Provider:
        def get_option_chain_normalized(self, *_args, **_kwargs):
            return _chain().assign(timestamp='2026-08-01T00:00:00Z')

    assert refresh_option_chain(
        Provider(), 'US.AAPL', max_age_seconds=60,
        now='2026-09-01T00:00:00Z', monotonic=iter([0, 0.1]).__next__,
    ) is None

    class CallerTimestampProvider:
        def get_option_chain_normalized(self, *_args, **kwargs):
            # 没有源 timestamp，normalize_option_chain 会标记 caller 来源。
            from common.options.chain import normalize_option_chain
            return normalize_option_chain(
                _chain(), 'US.AAPL', timestamp=kwargs.get('timestamp'),
            )

    assert refresh_option_chain(
        CallerTimestampProvider(), 'US.AAPL', as_of='2026-09-01T00:00:00Z',
        max_age_seconds=60, now='2026-09-01T00:00:00Z',
        monotonic=iter([0, 0.1]).__next__,
    ) is None


def test_delta_hedge_respects_market_and_turnover_guards():
    plan = compute_delta_hedge('US.AAPL', -250, max_shares=100, lot_size=10)
    assert plan.capped_shares == 100
    blocked = compute_delta_hedge('US.AAPL', -25, max_shares=100, lot_size=1, market_open=False)
    assert blocked.blocked and blocked.reason == 'market_closed'
    turnover = compute_delta_hedge('US.AAPL', -25, max_shares=100, max_turnover=10)
    assert turnover.blocked and turnover.reason == 'max_turnover'
