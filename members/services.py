import phonenumbers
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from core.errors import RuleViolation
from core.http import clean_query
from core.money import clean_reason
from core.sequences import next_value

from .models import Member

DEFAULT_REGION = "IN"  # UI convenience only; the picker always shows the country


def format_member_no(number):
    return f"COV-M-{number:06d}"


def country_choices():
    """(region code, label with calling code) for the country picker."""
    rows = []
    for region in sorted(phonenumbers.SUPPORTED_REGIONS):
        code = phonenumbers.country_code_for_region(region)
        rows.append((region, f"{region} (+{code})"))
    return rows


def normalise_mobile(national_number, region):
    """Return the E.164 form or raise RuleViolation. Keeps validation in one place."""
    try:
        parsed = phonenumbers.parse(national_number, region)
    except phonenumbers.NumberParseException:
        raise RuleViolation(
            "mobile_invalid", "Enter a valid mobile number for that country."
        ) from None
    if not phonenumbers.is_valid_number(parsed):
        raise RuleViolation("mobile_invalid", "Enter a valid mobile number for that country.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _mobile_in_use(mobile_e164, exclude_pk=None):
    qs = Member.objects.filter(mobile_e164=mobile_e164)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


@transaction.atomic
def create_member(*, actor, full_name, national_number, region, email="", joined_on=None, notes=""):
    mobile = normalise_mobile(national_number, region)
    if _mobile_in_use(mobile):
        raise RuleViolation("mobile_duplicate", "A member with this mobile number already exists.")
    try:
        with transaction.atomic():  # savepoint: a lost race must not poison the outer transaction
            member = Member.objects.create(
                membership_no=format_member_no(next_value("member")),
                full_name=full_name.strip(),
                mobile_e164=mobile,
                email=email.strip(),
                joined_on=joined_on or timezone.localdate(),
                notes=notes,
            )
    except IntegrityError:
        # Another request registered the same mobile between our check and the insert.
        raise RuleViolation(
            "mobile_duplicate", "A member with this mobile number already exists."
        ) from None
    record(
        "member.create",
        actor=actor,
        entity_type="member",
        entity_id=member.membership_no,
        summary={"status": member.status},
    )
    return member


@transaction.atomic
def update_member(member, *, actor, full_name, national_number, region, email="", notes=""):
    if member.anonymised_at:
        raise RuleViolation("member_anonymised", "An anonymised member cannot be edited.")
    mobile = normalise_mobile(national_number, region)
    if _mobile_in_use(mobile, exclude_pk=member.pk):
        raise RuleViolation("mobile_duplicate", "A member with this mobile number already exists.")
    changed = [
        f
        for f, new in (
            ("full_name", full_name.strip()),
            ("mobile_e164", mobile),
            ("email", email.strip()),
            ("notes", notes),
        )
        if getattr(member, f) != new
    ]
    member.full_name = full_name.strip()
    member.mobile_e164 = mobile
    member.email = email.strip()
    member.notes = notes
    try:
        with transaction.atomic():
            member.save()
    except IntegrityError:
        raise RuleViolation(
            "mobile_duplicate", "A member with this mobile number already exists."
        ) from None
    record(
        "member.update",
        actor=actor,
        entity_type="member",
        entity_id=member.membership_no,
        summary={"changed_fields": changed},
    )
    return member


def _set_status(member, new_status, *, actor, action, summary=None):
    old = member.status
    member.status = new_status
    member.save(update_fields=["status", "needs_new_subscription", "updated_at"])
    record(
        action,
        actor=actor,
        entity_type="member",
        entity_id=member.membership_no,
        summary={"from": old, "to": new_status, **(summary or {})},
    )


@transaction.atomic
def suspend_member(member, *, actor):
    if member.status != Member.Status.ACTIVE:
        raise RuleViolation("member_not_active", "Only an active member can be suspended.")
    _set_status(member, Member.Status.SUSPENDED, actor=actor, action="member.suspend")


@transaction.atomic
def archive_member(member, *, actor):
    if member.status == Member.Status.ARCHIVED:
        raise RuleViolation("member_archived", "The member is already archived.")
    _set_status(member, Member.Status.ARCHIVED, actor=actor, action="member.archive")


REACTIVATION_MODES = ("restore", "new_subscription")


@transaction.atomic
def reactivate_member(member, *, actor, mode):
    """Owner chooses: restore the previous arrangement, or require a new subscription (B23)."""
    if mode not in REACTIVATION_MODES:
        raise RuleViolation("reactivation_mode", "Choose restore or new subscription.")
    if member.anonymised_at:
        raise RuleViolation("member_anonymised", "An anonymised member cannot be reactivated.")
    if member.status == Member.Status.ACTIVE:
        raise RuleViolation("member_active", "The member is already active.")
    member.needs_new_subscription = mode == "new_subscription"
    _set_status(
        member,
        Member.Status.ACTIVE,
        actor=actor,
        action="member.reactivate",
        summary={"mode": mode},
    )


# Other apps register callables(member, actor=...) run INSIDE the anonymisation transaction
# (the notification app erases the member's emails there), so a failure rolls the whole
# anonymisation back.
ANONYMISATION_HOOKS = []


@transaction.atomic
def anonymise_member(member, *, actor, reason, confirm):
    """Clear personal data in the live database only; history and the member ID remain.

    Backups made earlier keep the original data until their normal retention expiry.
    """
    if not confirm:
        raise RuleViolation("confirmation_required", "Explicit confirmation is required.")
    reason = clean_reason(reason, "A reason", required=True)
    # Lock the member row first, like every payment, token and subscription operation, so this
    # cannot interleave with one in flight; then work on the fresh row, not a stale object.
    locked = Member.objects.select_for_update().get(pk=member.pk)
    if locked.anonymised_at:
        raise RuleViolation("member_anonymised", "The member is already anonymised.")
    member.refresh_from_db()
    member.full_name = "Anonymised member"
    member.mobile_e164 = f"anon-{member.membership_no}"
    member.email = ""
    member.notes = ""
    member.status = Member.Status.ARCHIVED
    member.anonymised_at = timezone.now()
    member.save()
    for hook in ANONYMISATION_HOOKS:
        hook(member, actor=actor)
    record(
        "member.anonymise",
        actor=actor,
        entity_type="member",
        entity_id=member.membership_no,
        summary={"reason": reason},
    )


def search_members(query="", status=""):
    qs = Member.objects.all()
    query = clean_query(query)
    if query:
        from django.db.models import Q

        qs = qs.filter(
            Q(full_name__icontains=query)
            | Q(membership_no__icontains=query)
            | Q(mobile_e164__icontains=query.replace(" ", ""))
        )
    if status in Member.Status.values:
        qs = qs.filter(status=status)
    return qs
