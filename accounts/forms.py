from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm


class OwnerAuthenticationForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "throttled": "Too many failed attempts. Please try again in %(minutes)d minute(s).",
    }


class RecoveryForm(forms.Form):
    username = forms.CharField(max_length=150)
    code = forms.CharField(max_length=20, label="Recovery code")
    new_password1 = forms.CharField(widget=forms.PasswordInput, label="New password")
    new_password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm new password")

    def clean(self):
        data = super().clean()
        if data.get("new_password1") != data.get("new_password2"):
            raise forms.ValidationError("The two password fields did not match.")
        return data

    def validate_password_for(self, user):
        form = SetPasswordForm(
            user,
            {
                "new_password1": self.cleaned_data["new_password1"],
                "new_password2": self.cleaned_data["new_password2"],
            },
        )
        if not form.is_valid():
            for errors in form.errors.values():
                for e in errors:
                    self.add_error("new_password1", e)
            return None
        return form


__all__ = ["OwnerAuthenticationForm", "RecoveryForm", "get_user_model"]
