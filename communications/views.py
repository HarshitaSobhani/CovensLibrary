from urllib.parse import quote

from django.conf import settings
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.services import record
from core.models import LibrarySetting
from core.money import MAX_REASON_LENGTH
from core.templatetags.money import rupees
from members.models import Member

from . import messages, services
from .models import MessageTemplate, Notification

FILTERS = {
    "all": None,
    "open": ["prepared", "smtp_failed"],
    "problem": ["smtp_failed", "failed"],
    "accepted": ["smtp_accepted"],
    "skipped": ["skipped"],
}


@login_required
def notification_list(request):
    show = request.GET.get("status", "all")
    show = show if show in FILTERS else "all"
    qs = Notification.objects.select_related("member")
    if FILTERS[show]:
        qs = qs.filter(status__in=FILTERS[show])
    elif request.GET.get("hide_system") != "0":
        qs = qs.exclude(skip_code="not_notified")  # events with no email are bookkeeping
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "communications/list.html",
        {"nav_active": "reminders", "page": page, "show": show, "filters": FILTERS},
    )


@login_required
def notification_detail(request, pk):
    note = get_object_or_404(Notification.objects.select_related("member"), pk=pk)
    return render(
        request,
        "communications/detail.html",
        {
            "nav_active": "reminders",
            "note": note,
            "attempts": note.attempts.all(),
            "next_retry": services.next_retry_label(note),
        },
    )


@login_required
@require_POST
def process_now(request):
    result = services.run_cycle()
    flash.success(
        request,
        f"Processed: {result['consumed']} new event(s), {result['reminders']} reminder(s), "
        f"sent {sum(result['sent'].values())}.",
    )
    return redirect("communications:list")


@login_required
@require_POST
def test_email(request):
    to = (request.user.email or "").strip()
    if not to:
        flash.error(request, "Your owner account has no email address; add one first.")
        return redirect("communications:list")
    try:
        EmailMessage(
            subject="Coven's Library test email",
            body="This is a test of the SMTP settings.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
        ).send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - shown to the owner, never swallowed silently
        flash.error(
            request, f"The test email failed: {services._scrub(f'{type(exc).__name__}: {exc}')}"
        )
    else:
        flash.success(
            request, f"SMTP accepted the test email for {to}. This does not prove delivery."
        )
    record("smtp.test", request.user, "Notification", "", {})
    return redirect("communications:list")


# ---------------------------------------------------------------- WhatsApp (manual)
def template_text(key):
    row = MessageTemplate.objects.filter(key=key).first()
    return row.body if row else messages.DEFAULT_TEMPLATES[key]


def whatsapp_values(member):
    from circulation.models import Loan
    from finance.models import FinancialTransaction
    from memberships.models import MemberSubscription

    sub = (
        MemberSubscription.objects.filter(member=member, cancelled_at__isnull=True)
        .order_by("-expires_on")
        .first()
    )
    loan = (
        Loan.objects.filter(member=member, status="active").select_related("copy__edition").first()
    )
    last = (
        FinancialTransaction.objects.filter(member=member)
        .exclude(kind="reversal")
        .order_by("-id")
        .first()
    )
    refund = (
        FinancialTransaction.objects.filter(member=member, kind="token_refund")
        .order_by("-id")
        .first()
    )
    return {
        "name": member.full_name,
        "membership_no": member.membership_no,
        "library": LibrarySetting.load().library_name,
        "expiry_date": sub.expires_on.strftime("%-d %B %Y") if sub else "-",
        "due_date": loan.due_on.strftime("%-d %B %Y") if loan else "-",
        "title": loan.copy.edition.title if loan else "-",
        "amount": rupees(last.amount_paise) if last else "-",
        "_refund_amount": rupees(refund.amount_paise) if refund else "-",
    }


def whatsapp_url(member, text):
    digits = "".join(ch for ch in member.mobile_e164 if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(text)}"


@login_required
def whatsapp(request, member_pk):
    member = get_object_or_404(Member, pk=member_pk)
    key = request.GET.get("template", "welcome")
    if key not in messages.DEFAULT_TEMPLATES:
        key = "welcome"
    refusal = ""
    if member.anonymised_at:
        refusal = "This member was anonymised; there is no number to message."
    values = whatsapp_values(member)
    if key == "refund":
        values["amount"] = values["_refund_amount"]
    text = messages.render_whatsapp(template_text(key), values)
    return render(
        request,
        "communications/whatsapp.html",
        {
            "nav_active": "reminders",
            "member": member,
            "key": key,
            "keys": list(messages.DEFAULT_TEMPLATES),
            "text": text,
            "link": "" if refusal else whatsapp_url(member, text),
            "refusal": refusal,
        },
    )


@login_required
def templates(request):
    if request.method == "POST":
        key = request.POST.get("key", "")
        body = request.POST.get("body", "").strip()
        error = ""
        if key not in messages.DEFAULT_TEMPLATES:
            error = "Unknown template."
        elif not body:
            error = "The message cannot be empty."
        elif len(body) > MAX_REASON_LENGTH:
            error = f"The message is longer than {MAX_REASON_LENGTH} characters."
        elif messages.unknown_placeholders(body):
            error = "Unknown placeholder(s): " + ", ".join(messages.unknown_placeholders(body))
        if error:
            flash.error(request, error)
        else:
            MessageTemplate.objects.update_or_create(
                key=key, defaults={"body": body, "updated_at": timezone.now()}
            )
            record("whatsapp.template_edited", request.user, "MessageTemplate", key, {})
            flash.success(request, "Template saved.")
        return redirect("communications:templates")
    rows = [(k, template_text(k)) for k in messages.DEFAULT_TEMPLATES]
    return render(
        request,
        "communications/templates.html",
        {"nav_active": "reminders", "rows": rows, "placeholders": messages.PLACEHOLDERS},
    )
