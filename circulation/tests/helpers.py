from datetime import date

from catalogue import services as catalogue
from catalogue.models import Category
from finance.services import Entry
from memberships import services as ms
from memberships.tests.helpers import PW, make_owner, register

__all__ = ["PW", "make_owner", "register", "make_copies", "member_at", "D", "Entry", "ms"]

D = date


def make_copies(owner, n=2, title="A Book"):
    category, _ = Category.objects.get_or_create(name="Fiction")
    edition = catalogue.create_edition(
        author="Test Author", actor=owner, title=title, category=category
    )
    return catalogue.add_copies(edition, n, actor=owner, source="purchased")


REGISTERED_ON = D(2026, 11, 11)


def member_at(owner, name="Asha Rao", number="9876543210", plan="6m", today=REGISTERED_ON, **kw):
    """A registered member whose 6-month subscription runs 11 Nov 2026 - 10 May 2027."""
    return register(owner, plan=plan, name=name, number=number, today=today, **kw).member


ISSUED_ON = D(2026, 12, 1)


def overdue_loan(owner, issued_on=ISSUED_ON, name="Asha Rao", number="9876543210", copy=None):
    """A member with an active loan issued 1 Dec 2026, due 31 Dec 2026 (grace until 7 Jan 2027,
    'day 8' = 8 Jan 2027)."""
    from circulation import services

    member = member_at(owner, name, number)
    copy = copy or make_copies(owner, 1, title=f"B-{number}")[0]
    loan = services.issue_loan(actor=owner, member=member, copy=copy, today=issued_on)
    return member, copy, loan
