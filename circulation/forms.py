from django import forms
from django.urls import reverse

from catalogue.models import BookCopy
from core.forms import BootstrapFormMixin
from core.money import MAX_REASON_LENGTH
from members.models import Member


def _reason(label, required=False):
    return forms.CharField(
        label=label,
        required=required,
        max_length=MAX_REASON_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class BackdatingFields(forms.Form):
    backdate_reason = _reason("Reason for a past date")
    backdate_confirmed = forms.BooleanField(
        required=False,
        label="I confirm this carries a past date (it can change due dates and token forfeiture)",
    )


class IssueForm(BootstrapFormMixin, BackdatingFields):
    # A drop-down of members (never anonymised ones). The value is the membership number.
    member_no = forms.ModelChoiceField(
        queryset=Member.objects.none(),
        to_field_name="membership_no",
        label="Member",
        error_messages={"invalid_choice": "No member has this ID."},
    )
    copy_code = forms.CharField(
        label="Copy: type at least 3 letters of the title, or the copy ID or barcode",
        max_length=40,
        widget=forms.TextInput(
            attrs={"autocomplete": "off", "placeholder": "e.g. Hobbit or COV-C-000001"}
        ),
    )
    issued_on = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Issue date (blank = today)",
    )
    due_on = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Due date (blank = 30 days; only an earlier date may be chosen)",
    )
    notes = _reason("Notes")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        members = Member.objects.filter(anonymised_at__isnull=True).order_by("membership_no")
        self.fields["member_no"].queryset = members
        self.fields["member_no"].label_from_instance = lambda m: (
            f"{m.membership_no} — {m.full_name}"
        )
        self.fields["copy_code"].widget.attrs["data-copy-search"] = reverse(
            "circulation:copy_search"
        )

    def clean_member_no(self):
        member = self.cleaned_data["member_no"]
        self.member = member
        return member.membership_no

    def clean_copy_code(self):
        value = self.cleaned_data["copy_code"].strip()
        copy = (
            BookCopy.objects.filter(copy_code__iexact=value).first()
            or BookCopy.objects.filter(barcode__iexact=value).first()
        )
        if copy is None:
            raise forms.ValidationError("No copy has this ID or barcode.")
        self.copy = copy
        return value


class DecisionFields(forms.Form):
    owner_confirmed = forms.BooleanField(
        required=False,
        label="I confirm: the full token is forfeited and the copy leaves circulation "
        "(not needed for a Good return)",
    )
    reason = _reason("Reason for the decision (mandatory for Poor, Damaged and Lost)")


class ReturnForm(BootstrapFormMixin, DecisionFields, BackdatingFields):
    returned_on = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Return date (blank = today)",
    )
    condition = forms.ChoiceField(
        choices=[
            ("good", "Good - back on the shelf"),
            ("poor", "Poor - condition Poor, status Withdrawn"),
            ("damaged", "Damaged - removed from circulation"),
        ],
        label="Condition of the copy",
    )
    notes = _reason("Notes")


class LostForm(BootstrapFormMixin, DecisionFields, BackdatingFields):
    lost_on = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Date lost (blank = today)",
    )
    notes = _reason("Notes")


class RenewForm(BootstrapFormMixin, forms.Form):
    notes = _reason("Notes")


class GraceForm(BootstrapFormMixin, forms.Form):
    grace_days = forms.IntegerField(min_value=0, max_value=365, label="Grace period (days)")


class DecisionForm(BootstrapFormMixin, forms.Form):
    reason = _reason("Reason (mandatory)", required=True)
