"""C1 monetary boundaries; no ambient Decimal context is inherited."""

from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from functools import wraps
import math

from trading_runtime.config import SafetyError

CENT = Decimal("0.01")


def money_context():
    context = Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-999999,
                      Emax=999999, capitals=1, clamp=0,
                      traps=[InvalidOperation, DivisionByZero, Overflow])
    context.clear_flags()
    return localcontext(context)


def monetary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with money_context():
            return function(*args, **kwargs)
    return wrapped


def exact_decimal(text):
    if not isinstance(text, str):
        raise SafetyError("D1_PRICE_REQUIRES_DECIMAL_TEXT")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise SafetyError("D1_INVALID_DECIMAL") from None
    if not value.is_finite():
        raise SafetyError("D1_NONFINITE")
    return value


def decimal_from_f64(value):
    value = float(value)
    if not math.isfinite(value):
        raise SafetyError("D1_NONFINITE")
    return Decimal(repr(value))


@monetary
def floor_cent(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SafetyError("D1_MONETARY_TYPE")
    return value.quantize(CENT, rounding=ROUND_FLOOR)


@monetary
def ceil_cent(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SafetyError("D1_MONETARY_TYPE")
    return value.quantize(CENT, rounding=ROUND_CEILING)
