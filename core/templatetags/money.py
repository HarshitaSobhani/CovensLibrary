from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from django import template

register = template.Library()


@register.filter
def rupees(paise):
    """Integer paise (or an exact Fraction) -> '₹1,500.00'. Rounds half-up, for display only."""
    if paise in (None, ""):
        return ""
    value = Fraction(paise)
    amount = (Decimal(value.numerator) / Decimal(value.denominator) / 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return f"₹{amount:,.2f}"
