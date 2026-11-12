from django import forms
from django.utils import timezone

from core.errors import RuleViolation
from core.forms import BootstrapFormMixin
from core.money import MAX_REASON_LENGTH

from . import services


class MemberForm(BootstrapFormMixin, forms.Form):
    full_name = forms.CharField(max_length=200)
    region = forms.ChoiceField(choices=services.country_choices, label="Country")
    national_number = forms.CharField(max_length=40, label="Mobile number")
    email = forms.EmailField(required=False)
    joined_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, member=None, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.initial.get("region"):
            self.initial["region"] = services.DEFAULT_REGION
        if member is not None:
            self.fields.pop("joined_on")
        elif not self.is_bound:
            self.initial["joined_on"] = timezone.localdate()

    def clean(self):
        data = super().clean()
        region = data.get("region")
        number = data.get("national_number")
        if region and number:
            try:
                mobile = services.normalise_mobile(number, region)
            except RuleViolation as exc:
                self.add_error("national_number", exc.message)
            else:
                if services._mobile_in_use(
                    mobile, exclude_pk=self.member.pk if self.member else None
                ):
                    self.add_error(
                        "national_number", "A member with this mobile number already exists."
                    )
        return data


class AnonymiseForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(max_length=MAX_REASON_LENGTH, widget=forms.Textarea(attrs={"rows": 2}))
    confirm = forms.BooleanField(
        label="I understand this clears the member's personal data permanently in the live database"
    )


class ReactivateForm(BootstrapFormMixin, forms.Form):
    mode = forms.ChoiceField(
        choices=[
            ("restore", "Restore the previous membership arrangement"),
            ("new_subscription", "Require a new subscription"),
        ],
        widget=forms.RadioSelect,
    )
