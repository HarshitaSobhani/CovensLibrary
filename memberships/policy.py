"""Subscription date rules. ALL date arithmetic for memberships lives in this module.

Pure functions, no database access: ``today`` is always passed in. Conventions (decisions A1, B1,
B2, B3, B4, B5, B6, A2, B18):

* A subscription's start date is inclusive. ``expires_on`` is the FIRST EXPIRED day; the last
  valid day is ``expires_on - 1``.
* ``expires_on`` = start + N calendar months, clamped to the target month's last day.
* A loan due date (at issue or renewal) must be on or before the last valid day.
* New loans are blocked from ``last_valid_day - 15`` onwards. The block and the due-date cap are
  independent; both are enforced.
"""

import calendar
from datetime import date, timedelta
from typing import NamedTuple

from core.errors import RuleViolation

ISSUE_BLOCK_DAYS = 15  # default of LibrarySetting.issue_block_days

SCHEDULED, ACTIVE, EXPIRED, CANCELLED = "scheduled", "active", "expired", "cancelled"


class Period(NamedTuple):
    """A subscription period: ``starts_on`` inclusive, ``expires_on`` = first expired day."""

    starts_on: date
    expires_on: date
    cancelled: bool = False


def add_months(start, months):
    """Calendar-month addition; the day is clamped to the target month's last day."""
    if months < 0:
        raise ValueError("months must not be negative")
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def expires_on(start, months):
    """First expired day of a subscription starting on ``start`` for ``months`` months."""
    return add_months(start, months)


def last_valid_day(expiry):
    return expiry - timedelta(days=1)


def state(period, today):
    """scheduled / active / expired / cancelled, from dates alone (never stored)."""
    if period.cancelled:
        return CANCELLED
    if today < period.starts_on:
        return SCHEDULED
    if today < period.expires_on:
        return ACTIVE
    return EXPIRED


def is_valid_on(period, day):
    """True on every day from the start through the last valid day (not cancelled)."""
    return (not period.cancelled) and period.starts_on <= day < period.expires_on


def current_period(periods, today):
    """The one period that is active today, or None."""
    active = [p for p in periods if state(p, today) == ACTIVE]
    return max(active, key=lambda p: p.starts_on) if active else None


def scheduled_periods(periods, today):
    return sorted((p for p in periods if state(p, today) == SCHEDULED), key=lambda p: p.starts_on)


def periods_overlap(a, b):
    return a.starts_on < b.expires_on and b.starts_on < a.expires_on


# ---- starting the next subscription ----------------------------------------------------------
def next_start(periods, today, activation_date=None):
    """Start date of a new (renewal or first) subscription, or RuleViolation.

    * At most one scheduled future subscription (B5).
    * Early renewal (a subscription is active): starts on its first expired day (A1); an
      owner-entered activation date must then be empty or equal to that day.
    * Late renewal / first subscription: starts on the owner-entered activation date (B6), which
      must not overlap any earlier, non-cancelled period.
    """
    live = [p for p in periods if not p.cancelled]
    if scheduled_periods(live, today):
        raise RuleViolation(
            "scheduled_exists", "A future subscription is already scheduled for this member."
        )
    current = current_period(live, today)
    if current is not None:
        if activation_date is not None and activation_date != current.expires_on:
            raise RuleViolation(
                "activation_date_fixed",
                "An early renewal starts on the current subscription's first expired day "
                f"({current.expires_on:%d %b %Y}).",
            )
        return current.expires_on
    if activation_date is None:
        raise RuleViolation(
            "activation_date_required", "Enter the activation date for this subscription."
        )
    latest_end = max((p.expires_on for p in live), default=None)
    if latest_end is not None and activation_date < latest_end:
        raise RuleViolation(
            "activation_overlap",
            f"The activation date cannot be before {latest_end:%d %b %Y} "
            "(the earlier subscription would overlap).",
        )
    return activation_date


# ---- borrowing -------------------------------------------------------------------------------
def max_due_date(period):
    """A loan due date (issue or renewal) may not be later than this (the last valid day)."""
    return last_valid_day(period.expires_on)


def issue_blocked_from(period, block_days=ISSUE_BLOCK_DAYS):
    """First day on which NEW loans are blocked: last valid day - ``block_days``."""
    return last_valid_day(period.expires_on) - timedelta(days=block_days)


def is_issue_blocked(period, issue_date, block_days=ISSUE_BLOCK_DAYS):
    return issue_date >= issue_blocked_from(period, block_days)


def check_issue(period, issue_date, due_date, block_days=ISSUE_BLOCK_DAYS):
    """Raise RuleViolation unless a NEW loan may be issued. The two rules are independent."""
    if period is None or not is_valid_on(period, issue_date):
        raise RuleViolation("no_valid_subscription", "The member has no valid subscription.")
    if is_issue_blocked(period, issue_date, block_days):
        raise RuleViolation(
            "issue_window_closed",
            "New loans are not issued in the final "
            f"{block_days} days of a subscription (blocked from "
            f"{issue_blocked_from(period, block_days):%d %b %Y}). Renew the subscription first.",
        )
    check_due_date(period, due_date)


def check_due_date(period, due_date):
    if due_date > max_due_date(period):
        raise RuleViolation(
            "due_after_last_valid_day",
            f"The due date cannot be after the last valid day ({max_due_date(period):%d %b %Y}).",
        )


def check_loan_renewal(period, today, current_due, new_due):
    """Loan renewal: never after the due date (B18); never beyond the last valid day (A2)."""
    if today > current_due:
        raise RuleViolation(
            "renewal_after_due_date", "A loan cannot be renewed once its due date has passed."
        )
    if period is None or not is_valid_on(period, today):
        raise RuleViolation("no_valid_subscription", "The member has no valid subscription.")
    check_due_date(period, new_due)
