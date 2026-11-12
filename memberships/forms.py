import uuid

from django import forms
from django.utils import timezone

from core.forms import BootstrapFormMixin
from core.money import MAX_REASON_LENGTH
from finance.forms import PaymentMixin, rupees_field
from members.forms import MemberForm

from .models import SubscriptionPlan


class SubscriptionPaymentForm(BootstrapFormMixin, PaymentMixin, forms.Form):
    plan = forms.ChoiceField(choices=[])
    activation_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Activation date (required for a late renewal)",
    )
    sub_cash = rupees_field("Subscription: cash (₹)")
    sub_upi = rupees_field("Subscription: UPI (₹)")
    sub_upi_ref = forms.CharField(required=False, max_length=120, label="UPI reference")
    payment_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Payment date (blank = today)",
    )
    backdate_reason = forms.CharField(
        required=False,
        max_length=MAX_REASON_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Reason for a past date",
    )
    backdate_confirmed = forms.BooleanField(
        required=False, label="I confirm this financial record carries a past date"
    )
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].choices = [
            (p.code, f"{p.name} — ₹{p.price_paise / 100:,.2f}")
            for p in SubscriptionPlan.objects.filter(is_active=True)
        ]
        if not self.is_bound:
            self.initial["request_key"] = uuid.uuid4()


class RegistrationForm(SubscriptionPaymentForm, MemberForm):
    tok_cash = rupees_field("Token: cash (₹)")
    tok_upi = rupees_field("Token: UPI (₹)")
    tok_upi_ref = forms.CharField(required=False, max_length=120, label="Token UPI reference")

    field_order = [
        "full_name",
        "region",
        "national_number",
        "email",
        "joined_on",
        "notes",
        "plan",
        "activation_date",
        "sub_cash",
        "sub_upi",
        "sub_upi_ref",
        "tok_cash",
        "tok_upi",
        "tok_upi_ref",
        "payment_date",
        "backdate_reason",
        "backdate_confirmed",
        "request_key",
    ]

    def member_fields(self):
        keys = ("full_name", "region", "national_number", "email", "joined_on", "notes")
        return {
            k: self.cleaned_data.get(k) for k in keys if self.cleaned_data.get(k) not in (None,)
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["activation_date"] = timezone.localdate()


class RenewalForm(SubscriptionPaymentForm):
    pass


class FreshTokenForm(BootstrapFormMixin, PaymentMixin, forms.Form):
    tok_cash = rupees_field("Token: cash (₹)")
    tok_upi = rupees_field("Token: UPI (₹)")
    tok_upi_ref = forms.CharField(required=False, max_length=120, label="UPI reference")
    payment_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Payment date (blank = today)",
    )
    backdate_reason = forms.CharField(
        required=False,
        max_length=MAX_REASON_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Reason for a past date",
    )
    backdate_confirmed = forms.BooleanField(
        required=False, label="I confirm this financial record carries a past date"
    )
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["request_key"] = uuid.uuid4()


class CancelForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(max_length=MAX_REASON_LENGTH, widget=forms.Textarea(attrs={"rows": 2}))
    confirm = forms.BooleanField(
        label="I understand the subscription fee is never refunded and no credit is given"
    )
