import math

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from audit.services import record

from . import ratelimit
from .forms import OwnerAuthenticationForm, RecoveryForm
from .models import LoginEvent
from .recovery import consume_code
from .sessions import end_of_day


def client_ip(request):
    return request.META.get("REMOTE_ADDR") or None


def start_owner_session(request, user):
    """Log in and end the session at the end of the local day (re-login is always allowed)."""
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.set_expiry(end_of_day())


class OwnerLoginView(LoginView):
    form_class = OwnerAuthenticationForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = False

    def post(self, request, *args, **kwargs):
        username = (request.POST.get("username") or "").strip()
        wait = ratelimit.retry_after(username, client_ip(request))
        if wait is not None:
            LoginEvent.objects.create(
                username_tried=username[:150],
                outcome=LoginEvent.Outcome.THROTTLED,
                ip=client_ip(request),
            )
            record("auth.login_throttled", summary={"username": username[:150]})
            minutes = max(1, math.ceil(wait.total_seconds() / 60))
            form = self.get_form()
            form.add_error(None, form.error_messages["throttled"] % {"minutes": minutes})
            response = self.render_to_response(self.get_context_data(form=form))
            response.status_code = 429
            response["Retry-After"] = str(int(wait.total_seconds()))
            return response
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        start_owner_session(self.request, user)
        LoginEvent.objects.create(
            user=user,
            username_tried=user.get_username(),
            outcome=LoginEvent.Outcome.SUCCESS,
            ip=client_ip(self.request),
        )
        record("auth.login", actor=user, entity_type="user", entity_id=user.pk)
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        username = (self.request.POST.get("username") or "").strip()[:150]
        LoginEvent.objects.create(
            username_tried=username,
            outcome=LoginEvent.Outcome.FAILED,
            ip=client_ip(self.request),
        )
        record("auth.login_failed", summary={"username": username})
        return super().form_invalid(form)


@require_POST
def logout_view(request):
    user = request.user if request.user.is_authenticated else None
    if user:
        record("auth.logout", actor=user, entity_type="user", entity_id=user.pk)
    logout(request)
    return redirect("accounts:login")


def recover_view(request):
    """Reset the password with a single-use recovery code issued from the local command line."""
    form = RecoveryForm(request.POST or None)
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        wait = ratelimit.retry_after(username, client_ip(request))
        if wait is not None:
            LoginEvent.objects.create(
                username_tried=username[:150],
                outcome=LoginEvent.Outcome.THROTTLED,
                ip=client_ip(request),
            )
            form.add_error(None, "Too many failed attempts. Please try again later.")
            return render(request, "accounts/recover.html", {"form": form}, status=429)
        if form.is_valid():
            user = (
                get_user_model().objects.filter(username__iexact=username, is_active=True).first()
            )
            ok = False
            if user and form.validate_password_for(user) is not None:
                ok = consume_code(user, form.cleaned_data["code"])
                if ok:
                    user.set_password(form.cleaned_data["new_password1"])
                    user.save(update_fields=["password"])
                    record(
                        "auth.password_recovered", actor=user, entity_type="user", entity_id=user.pk
                    )
                    messages.success(request, "Password updated. You can sign in now.")
                    return redirect("accounts:login")
            if not form.errors:
                LoginEvent.objects.create(
                    username_tried=username[:150],
                    outcome=LoginEvent.Outcome.RECOVERY_FAILED,
                    ip=client_ip(request),
                )
                record("auth.recovery_failed", summary={"username": username[:150]})
                form.add_error(None, "That recovery code is invalid or has expired.")
    return render(request, "accounts/recover.html", {"form": form})
