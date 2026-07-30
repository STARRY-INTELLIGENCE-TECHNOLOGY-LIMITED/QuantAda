from common.order_quantity import align_quantity_down, quantity_at_most


def test_align_quantity_down_never_rounds_large_half_lot_up():
    assert align_quantity_down(1_000_000_000_000.5, 1) == 1_000_000_000_000


def test_align_quantity_down_preserves_tiny_crypto_lot_without_overbuying():
    assert align_quantity_down(10_000.000000005, 0.00000001) == 10_000


def test_position_reconciliation_does_not_hide_material_large_quantity_remainder():
    assert quantity_at_most(1_000_000_000_000.5, 1_000_000_000_000) is False


def test_position_reconciliation_tolerance_respects_tiny_lot_size():
    assert quantity_at_most(0.0000000000005, 0, 0.000000000000001) is False
