from datetime import date

from django.contrib.auth import get_user_model

from finance.services import Entry
from memberships import services as ms

PW = "Correct-Horse-9!"
TODAY = date(2026, 11, 11)


def make_owner():
    return get_user_model().objects.create_user("owner", password=PW)


def fields(name="Asha Rao", number="9876543210"):
    return {"full_name": name, "national_number": number, "region": "IN"}


def register(owner, plan="6m", name="Asha Rao", number="9876543210", today=TODAY, **kw):
    price = {"6m": 80000, "12m": 150000}[plan]
    kw.setdefault("subscription_entries", [Entry("cash", price)])
    kw.setdefault("token_entries", [Entry("upi", 50000, reference="U1")])
    return ms.register_member(
        actor=owner, member_fields=fields(name, number), plan_code=plan, today=today, **kw
    )


def renew(owner, member, plan="6m", today=TODAY, **kw):
    price = {"6m": 80000, "12m": 150000}[plan]
    kw.setdefault("entries", [Entry("cash", price)])
    return ms.renew_subscription(actor=owner, member=member, plan_code=plan, today=today, **kw)


def forfeit(owner, member, today=TODAY, trigger="lost"):
    from finance import corrections

    return corrections.forfeit_token(
        actor=owner,
        member=member,
        trigger=trigger,
        effective_date=today,
        owner_confirmed=True,
        reason="test",
        today=today,
    )


def ensure_plans():
    """TransactionTestCase flushes remove the migration-seeded plans; recreate them."""
    from memberships.models import SubscriptionPlan

    SubscriptionPlan.objects.get_or_create(
        code="6m",
        defaults={"name": "6-month subscription", "duration_months": 6, "price_paise": 80000},
    )
    SubscriptionPlan.objects.get_or_create(
        code="12m",
        defaults={"name": "12-month subscription", "duration_months": 12, "price_paise": 150000},
    )
