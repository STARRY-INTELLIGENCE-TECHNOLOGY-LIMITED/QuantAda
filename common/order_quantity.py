from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext


_ZERO = Decimal('0')
_DEFAULT_STEP = Decimal('1')


def decimal_quantity(value, *, absolute=False) -> Decimal:
    """Convert broker quantities through their decimal text representation."""
    try:
        quantity = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return _ZERO
    if not quantity.is_finite():
        return _ZERO
    return abs(quantity) if absolute else quantity


def quantity_number(value):
    """Return integral quantities as int and fractional quantities as float."""
    quantity = decimal_quantity(value)
    if quantity == quantity.to_integral_value():
        return int(quantity)
    return float(quantity)


def positive_quantity(value):
    quantity = decimal_quantity(value)
    return quantity_number(quantity if quantity > 0 else _ZERO)


def normalize_quantity_step(value, default=1):
    step = decimal_quantity(value, absolute=True)
    if step <= 0:
        step = decimal_quantity(default, absolute=True) or _DEFAULT_STEP
    return quantity_number(step)


def align_quantity_down(value, step):
    quantity = decimal_quantity(value, absolute=True)
    lot = decimal_quantity(step, absolute=True)
    if quantity <= 0 or lot <= 0:
        return 0
    with localcontext() as ctx:
        ctx.prec = 50
        raw_units = quantity / lot
        floor_units = raw_units.to_integral_value(rounding=ROUND_FLOOR)
        ceil_units = raw_units.to_integral_value(rounding=ROUND_CEILING)
        tolerance = min(
            Decimal('1e-7'),
            max(Decimal('1e-12'), abs(raw_units) * Decimal('1e-12')),
        )
        units = ceil_units if ceil_units - raw_units <= tolerance else floor_units
        return quantity_number(units * lot)


def subtract_quantities(left, right):
    with localcontext() as ctx:
        ctx.prec = 50
        result = decimal_quantity(left) - decimal_quantity(right)
    return quantity_number(result if result > 0 else _ZERO)


def sum_quantities(values):
    with localcontext() as ctx:
        ctx.prec = 50
        result = sum((decimal_quantity(value) for value in values), _ZERO)
    return quantity_number(result)


def quantity_chunk_plan(total, limit):
    """Return (order count, final order quantity) without materializing every chunk."""
    quantity = decimal_quantity(total, absolute=True)
    chunk = decimal_quantity(limit, absolute=True)
    if quantity <= 0:
        return 0, 0
    if chunk <= 0 or quantity <= chunk:
        return 1, quantity_number(quantity)
    with localcontext() as ctx:
        ctx.prec = 50
        count = int((quantity / chunk).to_integral_value(rounding=ROUND_CEILING))
        final = quantity - chunk * (count - 1)
    return count, quantity_number(final)


def quantity_at_most(value, target, step=None) -> bool:
    current = decimal_quantity(value)
    expected = decimal_quantity(target)
    scale = max(abs(current), abs(expected), Decimal('1'))
    tolerance = min(
        Decimal('1e-7'),
        max(Decimal('1e-12'), scale * Decimal('1e-12')),
    )
    lot = decimal_quantity(step, absolute=True)
    if lot > 0:
        tolerance = min(tolerance, lot / Decimal('2'))
    return current <= expected + tolerance


def format_quantity(value) -> str:
    quantity = decimal_quantity(value)
    if quantity == quantity.to_integral_value():
        return str(int(quantity))
    return format(quantity.normalize(), 'f')
