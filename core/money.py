"""Shared money and free-text limits (decision H1/H3). Money is integer paise everywhere.

* ``MAX_PAISE`` is the largest single amount accepted anywhere (Rs 49,999.00, owner decision
  D-16). Money columns stay 64-bit integers; the limit is enforced by a CHECK constraint on every
  money column as well as by service and form validation, so an oversized value is always a clear
  validation error, never a database overflow.
* ``MAX_REASON_LENGTH`` is the longest accepted reason / note (2,000 characters, D-16). Longer
  text is REJECTED, never silently shortened.
"""

from decimal import Decimal

from core.errors import RuleViolation

MAX_PAISE = 4_999_900  # Rs 49,999.00 (D-16)
MAX_RUPEES = Decimal(MAX_PAISE) / 100  # Decimal("49999"), for form fields
MAX_REASON_LENGTH = 2000  # D-16


def check_amount(value, what="The amount"):
    """Return ``value`` if it is a whole number of paise within 0..MAX_PAISE, else RuleViolation."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleViolation("amount_invalid", f"{what} must be a whole number of paise.")
    if value > MAX_PAISE:
        raise RuleViolation(
            "amount_too_large",
            f"{what} cannot be more than Rs {MAX_PAISE / 100:,.2f}.",
        )
    return value


def clean_reason(text, label="The reason", required=False):
    """Strip a reason/note; reject (never truncate) text above the limit."""
    text = (text or "").strip()
    if required and not text:
        raise RuleViolation("reason_required", f"{label} is required.")
    if len(text) > MAX_REASON_LENGTH:
        raise RuleViolation(
            "reason_too_long",
            f"{label} is {len(text)} characters; the limit is {MAX_REASON_LENGTH}.",
        )
    return text
