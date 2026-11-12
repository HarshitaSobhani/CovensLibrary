from decimal import Decimal

from django import forms

from core.forms import BootstrapFormMixin
from core.money import MAX_REASON_LENGTH, MAX_RUPEES

from .services import Entry


def rupees_field(label, required=False):
    return forms.DecimalField(
        label=label,
        required=required,
        min_value=Decimal("0"),
        max_value=MAX_RUPEES,
        max_digits=10,
        decimal_places=2,
    )


def reason_field(label="Reason"):
    return forms.CharField(
        label=label, max_length=MAX_REASON_LENGTH, widget=forms.Textarea(attrs={"rows": 2})
    )


def to_paise(value):
    return int((value * 100).to_integral_value()) if value else 0


class PaymentMixin:
    """Cash / UPI amounts in rupees for one obligation; converts them to ledger entries."""

    def entries(self, prefix, paid_on=None):
        out = []
        data = self.cleaned_data
        if data.get(f"{prefix}_cash"):
            out.append(Entry("cash", to_paise(data[f"{prefix}_cash"]), paid_on))
        if data.get(f"{prefix}_upi"):
            ref = (data.get(f"{prefix}_upi_ref") or "").strip()
            out.append(Entry("upi", to_paise(data[f"{prefix}_upi"]), paid_on, ref))
        return out


class MoneyOutForm(BootstrapFormMixin, PaymentMixin, forms.Form):
    """How a refund / outgoing correction was paid out (cash and/or UPI)."""

    pay_cash = rupees_field("Paid out: cash (₹)")
    pay_upi = rupees_field("Paid out: UPI (₹)")
    pay_upi_ref = forms.CharField(required=False, max_length=120, label="UPI reference")
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            import uuid

            self.initial["request_key"] = uuid.uuid4()


class RefundForm(MoneyOutForm):
    pass


class ReverseForm(MoneyOutForm):
    reason = reason_field()
    confirm = forms.BooleanField(
        label="I confirm this reversal; the original record stays and is never edited"
    )


class AdjustmentForm(MoneyOutForm):
    direction = forms.ChoiceField(
        choices=[("in", "Money in"), ("out", "Money out"), ("none", "No cash (record only)")]
    )
    amount = rupees_field("Amount (₹)", required=True)
    pay_cash = rupees_field("Cash (₹)")
    pay_upi = rupees_field("UPI (₹)")
    reason = reason_field()
    approved = forms.BooleanField(label="Owner approval: I approve this financial adjustment")
    confirm_not_fee_refund = forms.BooleanField(
        required=False,
        label="Money out only: I confirm this is NOT a refund of a subscription fee",
    )
