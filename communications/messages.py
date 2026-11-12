"""Email texts (plain text) and WhatsApp placeholder rendering. Pure functions: no database
writes, no sending. Names and titles never reach a header unsanitised."""

import re

from core.templatetags.money import rupees


def _d(value):
    return value.strftime("%-d %B %Y") if hasattr(value, "strftime") else str(value)


def one_line(text):
    return re.sub(r"[\r\n]+", " ", str(text)).strip()


def _sign(library):
    lines = [library.library_name]
    for extra in (library.library_address, library.library_phone, library.library_email):
        if extra:
            lines.append(extra)
    return "\n".join(["", "Regards,", *lines])


def compose(kind, *, member, library, ctx):
    """Return (subject, body). ``ctx`` holds only values the caller resolved from payloads and
    records. Missing values render as a dash so a gap never breaks an email."""
    name = member.full_name
    lib = library.library_name
    hello = f"Dear {name},\n\n"
    get = lambda key: ctx.get(key) if ctx.get(key) not in (None, "") else "-"  # noqa: E731
    if kind == "subscription_activation":
        subject = f"Your {lib} subscription is active"
        body = (
            f"{hello}Your {get('plan')} subscription is active from {_d(get('starts_on'))}. "
            f"It lasts until {_d(get('last_valid_day'))} (it expires on {_d(get('expires_on'))}).\n"
            f"Member ID: {member.membership_no}"
        )
    elif kind == "payment_receipt":
        lines = [
            f"  {r['receipt_no']}  {r['kind'].replace('_', ' ')}  {rupees(r['amount_paise'])}"
            for r in ctx.get("receipts", [])
        ]
        subject = "Payment receipt " + ", ".join(r["receipt_no"] for r in ctx.get("receipts", []))
        body = (
            f"{hello}We received your payment. Receipt{'s' if len(lines) > 1 else ''}:\n"
            + "\n".join(lines)
        )
    elif kind == "loan_issued":
        subject = f"Book issued: {get('title')}"
        body = (
            f"{hello}You borrowed {get('title')} (copy {get('copy_code')}). "
            f"Please return it by {_d(get('due_on'))}."
        )
    elif kind == "loan_renewed":
        subject = f"Loan renewed: {get('title')}"
        body = (
            f"{hello}Your loan of {get('title')} was renewed. "
            f"The new due date is {_d(get('new_due_on'))}."
        )
    elif kind == "loan_returned":
        subject = f"Book returned: {get('title')}"
        body = (
            f"{hello}We recorded the return of {get('title')} on {_d(get('returned_on'))}. "
            "Thank you."
        )
    elif kind == "token_forfeited":
        subject = f"Your {rupees(ctx.get('amount_paise'))} token was forfeited"
        body = (
            f"{hello}Your refundable token of {rupees(ctx.get('amount_paise'))} was forfeited on "
            f"{_d(get('effective_date'))}. Borrowing is paused until a fresh token is paid; "
            "your current subscription stays valid until it expires."
        )
    elif kind == "expiry_reminder":
        subject = f"Your {lib} subscription expires soon"
        body = (
            f"{hello}Your subscription expires on {_d(get('expires_on'))}; the last day you can "
            f"borrow is {_d(get('last_valid_day'))}. Please renew to continue."
        )
    elif kind == "subscription_expired":
        subject = f"Your {lib} subscription has expired"
        body = (
            f"{hello}Your subscription expired on {_d(get('expires_on'))}. "
            "Please renew to borrow again."
        )
    elif kind == "overdue_reminder":
        late = ctx.get("days_overdue", 0)
        if late:
            subject = f"Overdue book: {get('title')}"
            unit = "day" if late == 1 else "days"
            lead = f"{get('title')} is {late} {unit} overdue (due {_d(get('due_on'))})."
        else:
            subject = f"Book due today: {get('title')}"
            lead = f"{get('title')} is due today ({_d(get('due_on'))})."
        body = (
            f"{hello}{lead} Your grace period ends on {_d(get('grace_end'))}; "
            f"the token is forfeited automatically on {_d(get('forfeiture_day'))} if the book "
            "has not been returned."
        )
    else:
        raise ValueError(f"no email text for {kind!r}")
    return one_line(subject)[:300], body + "\n" + _sign(library)


# ---- WhatsApp (manual: the owner reviews and sends) -----------------------------------
PLACEHOLDERS = ("name", "membership_no", "library", "expiry_date", "due_date", "title", "amount")
DEFAULT_TEMPLATES = {
    "welcome": "Hello {{name}}, welcome to {{library}}! Your member ID is {{membership_no}}.",
    "expiry": (
        "Hello {{name}}, your {{library}} membership expires on {{expiry_date}}. "
        "Please renew to keep borrowing."
    ),
    "due": "Hello {{name}}, {{title}} is due on {{due_date}}. Please return or renew it.",
    "overdue": (
        "Hello {{name}}, {{title}} was due on {{due_date}} and is now overdue. "
        "Please return it soon."
    ),
    "payment": "Hello {{name}}, we received your payment of {{amount}}. Thank you! - {{library}}",
    "refund": "Hello {{name}}, your refund of {{amount}} has been recorded. - {{library}}",
}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def unknown_placeholders(text):
    return sorted({m for m in _PLACEHOLDER_RE.findall(text) if m not in PLACEHOLDERS})


def render_whatsapp(template, values):
    """Replace only whitelisted placeholders. Nothing else is evaluated."""
    return _PLACEHOLDER_RE.sub(
        lambda m: str(values.get(m.group(1), "")) if m.group(1) in PLACEHOLDERS else m.group(0),
        template,
    )
