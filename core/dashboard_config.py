"""Owner-configurable dashboard: section visibility, order and "members requiring attention"
criteria (PRD 6.1). Stored in ``LibrarySetting.dashboard_config``; every value is validated here,
unknown keys are dropped and missing keys fall back to the defaults, so a bad or old value can
never break the dashboard."""

SECTIONS = {
    "overdue": "Overdue books",
    "expiries": "Upcoming subscription expiries",
    "backup_failures": "Backup failures",
    "failed_emails": "Failed emails",
    "decisions": "Damaged / lost-book decisions",
    "forfeitures": "Token forfeitures",
    "attention": "Members requiring attention",
    "counts": "Library at a glance",
    "collections": "Monthly collections",
    "transactions": "Recent transactions",
}
DEFAULT_ORDER = list(SECTIONS)

# Default criteria accepted by the owner (PRD 6.1); each can be switched off.
CRITERIA = {
    "expired": "Subscription expired",
    "token_forfeited": "Borrowing suspended after token forfeiture",
    "overdue": "Overdue books",
    "damaged_lost": "Unresolved damaged or lost book cases",
    "pending_decision": "Pending owner decisions",
    "incomplete": "Incomplete membership record",
}
DEFAULT_EXPIRY_WINDOW_DAYS = 30
MAX_EXPIRY_WINDOW_DAYS = 365


def normalise(raw):
    """Return a complete, valid config from whatever is stored."""
    raw = raw if isinstance(raw, dict) else {}
    visible_raw = raw.get("visible") if isinstance(raw.get("visible"), dict) else {}
    criteria_raw = raw.get("criteria") if isinstance(raw.get("criteria"), dict) else {}
    order_raw = raw.get("order") if isinstance(raw.get("order"), list) else []
    order = [k for k in dict.fromkeys(order_raw) if k in SECTIONS]
    order += [k for k in DEFAULT_ORDER if k not in order]
    window = raw.get("expiry_window_days", DEFAULT_EXPIRY_WINDOW_DAYS)
    valid = isinstance(window, int) and not isinstance(window, bool)
    if not valid or not 1 <= window <= MAX_EXPIRY_WINDOW_DAYS:
        window = DEFAULT_EXPIRY_WINDOW_DAYS
    return {
        "visible": {k: bool(visible_raw.get(k, True)) for k in SECTIONS},
        "criteria": {k: bool(criteria_raw.get(k, True)) for k in CRITERIA},
        "order": order,
        "expiry_window_days": window,
    }


def load(setting):
    return normalise(setting.dashboard_config)


def move(order, key, direction):
    """Move one section up (-1) or down (+1); returns a new list."""
    order = list(order)
    if key not in order or direction not in (-1, 1):
        return order
    i = order.index(key)
    j = i + direction
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
    return order
