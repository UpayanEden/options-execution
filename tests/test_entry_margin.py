"""Broker funds and margin previews must block unsafe entries before any orders."""
from unittest.mock import MagicMock, patch

import pytest

from brokers.base import BrokerError
from brokers.zerodha import ZerodhaBroker
from tests.test_live_integration import _make_runner


def make_runner(tmp_path):
    runner, broker, _ = _make_runner(tmp_path=tmp_path)
    runner._get_spot = MagicMock(return_value=2800.0)
    return runner, broker


@pytest.mark.parametrize('preview', [{}, {'initial': {'total': 100}},
    {'initial': {'total': float('nan')}, 'final': {'total': 100}},
    {'initial': {'total': 100}, 'final': {'total': float('inf')}}])
def test_bad_preview_never_places_orders(tmp_path, preview):
    runner, broker = make_runner(tmp_path)
    broker.get_basket_margin = MagicMock(return_value=preview)
    assert runner.enter_condor(runner._config.stocks[0]) is None
    assert not broker._orders
    assert runner._capital_manager.get_total_allocated() == 0


def test_api_failure_never_uses_estimate(tmp_path):
    runner, broker = make_runner(tmp_path)
    broker.get_basket_margin = MagicMock(side_effect=BrokerError('timeout'))
    assert runner.enter_condor(runner._config.stocks[0]) is None
    assert not broker._orders


@pytest.mark.parametrize('net,cash', [(140416, 80086), (-1, 500000), (500000, 100)])
def test_full_requirement_and_cash_both_checked(tmp_path, net, cash):
    runner, broker = make_runner(tmp_path)
    broker._margins = {'equity': {'net': net, 'available': {
        'live_balance': cash, 'collateral': 1287259}}}
    broker.get_basket_margin = MagicMock(return_value={
        'initial': {'total': 394679}, 'final': {'total': 372215}})
    assert runner.enter_condor(runner._config.stocks[0]) is None
    assert not broker._orders


def test_peak_execution_requirement_exceeds_available_even_if_final_fits(tmp_path):
    runner, broker = make_runner(tmp_path)
    broker.get_basket_margin = MagicMock(return_value={
        'initial': {'total': 600000}, 'final': {'total': 10000}})
    assert runner.enter_condor(runner._config.stocks[0]) is None
    assert not broker._orders


def test_success_uses_real_shape_existing_positions_and_execution_order(tmp_path):
    runner, broker = make_runner(tmp_path)
    broker.get_basket_margin = MagicMock(return_value={
        'initial': {'total': 20000}, 'final': {'total': 18000}})
    with patch('live.condor_runner.time.sleep'):
        assert runner.enter_condor(runner._config.stocks[0]) is not None
    call = broker.get_basket_margin.call_args
    assert call.kwargs == {'consider_positions': True}
    preview = call.args[0]
    assert [o['transaction_type'] for o in preview] == ['BUY', 'BUY', 'SELL', 'SELL']
    assert [o['tradingsymbol'] for o in preview] == [o['tradingsymbol'] for o in broker._orders]
    assert runner._capital_manager.get_total_allocated() == 20000


@pytest.mark.parametrize('funds', [{}, {'equity': {'available': {'live_balance': 500000}}},
    {'equity': {'net': float('inf'), 'available': {'live_balance': 500000}}}])
def test_missing_or_invalid_funds_blocks_entry(tmp_path, funds):
    runner, broker = make_runner(tmp_path)
    broker._margins = funds
    assert runner.enter_condor(runner._config.stocks[0]) is None
    assert not broker._orders


def test_broker_preserves_default_standalone_and_supports_account_preview():
    broker = ZerodhaBroker.__new__(ZerodhaBroker)
    broker._kite = MagicMock()
    broker._kite.basket_order_margins.return_value = {
        'initial': {'total': 20000}, 'final': {'total': 18000}}
    broker.get_basket_margin([])
    broker._kite.basket_order_margins.assert_called_with([], consider_positions=False)
    broker.get_basket_margin([], consider_positions=True)
    broker._kite.basket_order_margins.assert_called_with([], consider_positions=True)
