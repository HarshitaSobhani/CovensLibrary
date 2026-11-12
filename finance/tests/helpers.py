from datetime import date

from django.contrib.auth import get_user_model

from finance import services as fs
from finance.services import Entry, Item
from members.services import create_member

PW = "Correct-Horse-9!"
K = fs.K
FLOW = fs.FLOW


def make_owner():
    return get_user_model().objects.create_user("owner", password=PW)


def make_member(owner, name="Asha Rao", number="9876543210"):
    return create_member(actor=owner, full_name=name, national_number=number, region="IN")


def sub_item(amount=80000, day=date(2026, 11, 11), entries=None, **kw):
    entries = entries if entries is not None else [Entry("cash", amount)]
    return Item(K.SUBSCRIPTION_PAYMENT, FLOW.IN, amount, day, entries, label="Subscription", **kw)


def tok_item(amount=50000, day=date(2026, 11, 11), entries=None, **kw):
    entries = entries if entries is not None else [Entry("upi", amount, reference="UPI-1")]
    return Item(K.TOKEN_COLLECTION, FLOW.IN, amount, day, entries, label="Token", **kw)
