"""Report date ranges (decision D-28a: CALENDAR week, quarter and year). Inclusive on both ends."""

from datetime import date, timedelta

PRESETS = {
    "today": "Today",
    "week": "This week (Mon-Sun)",
    "month": "This month",
    "quarter": "This quarter",
    "year": "This year",
    "custom": "Custom range",
}
MAX_SPAN_DAYS = 366 * 5


class PeriodError(ValueError):
    pass


def _last_of_month(year, month):
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def resolve(preset, today, start=None, end=None):
    """(start, end, label). ``start`` / ``end`` are ISO strings or dates, used only by 'custom'."""
    if preset == "today":
        return today, today, PRESETS["today"]
    if preset == "week":
        first = today - timedelta(days=today.weekday())
        return first, first + timedelta(days=6), PRESETS["week"]
    if preset == "month":
        return today.replace(day=1), _last_of_month(today.year, today.month), PRESETS["month"]
    if preset == "quarter":
        first_month = 3 * ((today.month - 1) // 3) + 1
        return (
            date(today.year, first_month, 1),
            _last_of_month(today.year, first_month + 2),
            PRESETS["quarter"],
        )
    if preset == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31), PRESETS["year"]
    if preset == "custom":
        try:
            first = start if isinstance(start, date) else date.fromisoformat(start or "")
            last = end if isinstance(end, date) else date.fromisoformat(end or "")
        except ValueError as exc:
            raise PeriodError("Enter the start and end dates as YYYY-MM-DD.") from exc
        if first > last:
            raise PeriodError("The start date must not be after the end date.")
        if (last - first).days > MAX_SPAN_DAYS:
            raise PeriodError("A custom range can span at most 5 years.")
        return first, last, "Custom range"
    raise PeriodError("Unknown period.")
