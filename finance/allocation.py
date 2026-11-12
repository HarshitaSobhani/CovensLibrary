"""Subscription-period revenue allocation: daily pro-rata at FULL precision.

Exact rational arithmetic (``fractions.Fraction``): the allocated parts of a subscription always
sum to exactly its price, and no per-day rounded amount exists anywhere. Rounding happens only in
``display_*``. Token collections are never part of this (they are not revenue, B21).
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction


def period_days(starts_on, expires_on):
    """Number of valid days: start inclusive through the last valid day (expiry exclusive)."""
    days = (expires_on - starts_on).days
    if days <= 0:
        raise ValueError("expires_on must be after starts_on")
    return days


def daily_amount(price_paise, starts_on, expires_on):
    return Fraction(price_paise, period_days(starts_on, expires_on))


def allocate(price_paise, starts_on, expires_on, from_date, to_date):
    """Paise (exact Fraction) of this subscription falling in [from_date, to_date], inclusive."""
    first = max(starts_on, from_date)
    last = min(expires_on - timedelta(days=1), to_date)
    overlap = (last - first).days + 1
    if overlap <= 0:
        return Fraction(0)
    return daily_amount(price_paise, starts_on, expires_on) * overlap


def allocate_by_month(price_paise, starts_on, expires_on):
    """{(year, month): exact paise}; the values sum exactly to ``price_paise``."""
    out, day, end = {}, starts_on, expires_on
    per_day = daily_amount(price_paise, starts_on, expires_on)
    while day < end:
        key = (day.year, day.month)
        out[key] = out.get(key, Fraction(0)) + per_day
        day += timedelta(days=1)
    return out


def display_rupees(paise):
    """Round half-up to 0.01 rupee. FOR DISPLAY ONLY; never feed the result back into sums."""
    value = Decimal(Fraction(paise).numerator) / Decimal(Fraction(paise).denominator) / 100
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def month_range(year, month):
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)
