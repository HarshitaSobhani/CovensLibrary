import math

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.views import LoginView, PasswordResetConfirmView, PasswordResetView
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from audit.services import record

from . import ratelimit
from .forms import OwnerAuthenticationForm, RecoveryForm
from .identity import audit_username, key_for, known_username
from .locks import advisory_locks
from .models import LoginEvent
from .recovery import consume_code
from .sessions import end_of_day


def client_ip(request):
    return request.META.get("REMOTE_ADDR") or None


def start_owner_session(request, user):
    """Log in and end the session at the end of the local day (re-login is always allowed)."""
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.set_expiry(end_of_day())


def log_event(outcome, request, typed="", user=None):
    """Record an authentication event without ever storing what was typed as a username."""
    return LoginEvent.objects.create(
        user=user,
        username_tried=user.get_username() if user else known_username(typed),
        username_key=key_for(user.get_username() if user else typed),
        outcome=outcome,
        ip=client_ip(request),
    )


class OwnerLoginView(LoginView):
    form_class = OwnerAuthenticationForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = False

    def post(self, request, *args, **kwargs):
        typed = (request.POST.get("username") or "").strip()
        ukey, ip = key_for(typed), client_ip(request)
        # One transaction holds the limiter locks across check -> authenticate -> record, so
        # simultaneous requests are evaluated one after another and cannot exceed the limit.
        with transaction.atomic():
            advisory_locks(*ratelimit.lock_keys(ukey, ip))
            wait = ratelimit.retry_after(ukey, ip)
            if wait is not None:
                log_event(LoginEvent.Outcome.THROTTLED, request, typed)
                record("auth.login_throttled", summary={"username": audit_username(typed)})
                minutes = max(1, math.ceil(wait.total_seconds() / 60))
                form = self.get_form()
                form.add_error(None, form.error_messages["throttled"] % {"minutes": minutes})
                response = self.render_to_response(self.get_context_data(form=form))
                response.status_code = 429
                response["Retry-After"] = str(int(wait.total_seconds()))
                return response
            response = super().post(request, *args, **kwargs)
            if hasattr(response, "render") and callable(response.render):
                response.render()  # render inside the transaction so any error rolls back cleanly
            return response

    def form_valid(self, form):
        user = form.get_user()
        start_owner_session(self.request, user)
        log_event(LoginEvent.Outcome.SUCCESS, self.request, user=user)
        record("auth.login", actor=user, entity_type="user", entity_id=user.pk)
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        typed = (self.request.POST.get("username") or "").strip()
        log_event(LoginEvent.Outcome.FAILED, self.request, typed)
        record("auth.login_failed", summary={"username": audit_username(typed)})
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
        typed = (request.POST.get("username") or "").strip()
        ukey, ip = key_for(typed), client_ip(request)
        with transaction.atomic():
            advisory_locks(*ratelimit.lock_keys(ukey, ip))
            wait = ratelimit.retry_after(ukey, ip)
            if wait is not None:
                log_event(LoginEvent.Outcome.THROTTLED, request, typed)
                form.add_error(None, "Too many failed attempts. Please try again later.")
                return render(request, "accounts/recover.html", {"form": form}, status=429)
            if form.is_valid():
                user = (
                    get_user_model().objects.filter(username__iexact=typed, is_active=True).first()
                )
                if user and form.validate_password_for(user) is not None:
                    if consume_code(user, form.cleaned_data["code"]):
                        user.set_password(form.cleaned_data["new_password1"])
                        user.save(update_fields=["password"])
                        record(
                            "auth.password_recovered",
                            actor=user,
                            entity_type="user",
                            entity_id=user.pk,
                        )
                        messages.success(request, "Password updated. You can sign in now.")
                        return redirect("accounts:login")
                if not form.errors:
                    log_event(LoginEvent.Outcome.RECOVERY_FAILED, request, typed)
                    record("auth.recovery_failed", summary={"username": audit_username(typed)})
                    form.add_error(None, "That recovery code is invalid or has expired.")
    return render(request, "accounts/recover.html", {"form": form})


class OwnerPasswordResetView(PasswordResetView):
    """Email-based reset with auditing and throttling (3 requests / 15 minutes per email or host).

    Every request is audited (never the address, token or link). A throttled request gets the same
    generic response as a normal one and sends no email, so the throttle reveals nothing.
    """

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        ekey, ip = key_for(email), client_ip(self.request)
        with transaction.atomic():
            advisory_locks(*ratelimit.lock_keys(ekey, ip, prefix="reset"))
            wait = ratelimit.reset_retry_after(ekey, ip)
            if wait is not None:
                LoginEvent.objects.create(
                    username_key=ekey, outcome=LoginEvent.Outcome.RESET_THROTTLED, ip=ip
                )
                record("auth.password_reset_throttled")
                return redirect(self.get_success_url())
            matched = sum(1 for _ in form.get_users(email))
            LoginEvent.objects.create(
                username_key=ekey, outcome=LoginEvent.Outcome.RESET_REQUESTED, ip=ip
            )
            record("auth.password_reset_requested", summary={"accounts_matched": matched})
        return super().form_valid(form)  # sends the email (outside the lock)


class OwnerPasswordResetConfirmView(PasswordResetConfirmView):
    def form_valid(self, form):
        response = super().form_valid(form)
        record(
            "auth.password_reset_completed",
            actor=form.user,
            entity_type="user",
            entity_id=form.user.pk,
        )
        return response
