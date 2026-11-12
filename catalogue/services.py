import re

from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.errors import RuleViolation
from core.http import clean_query
from core.sequences import next_value

from .models import Acquisition, BookCopy, BookEdition, Category, CopyEvent

UNKNOWN_AUTHOR = "Unknown author"


# ---------- ISBN ----------
def normalise_isbn(raw):
    return re.sub(r"[\s-]", "", (raw or "")).upper()


def _isbn10_ok(s):
    if not re.fullmatch(r"\d{9}[\dX]", s):
        return False
    total = sum((10 if ch == "X" else int(ch)) * (10 - i) for i, ch in enumerate(s))
    return total % 11 == 0


def _isbn13_ok(s):
    if not re.fullmatch(r"\d{13}", s):
        return False
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(s[:12]))
    return (10 - total % 10) % 10 == int(s[12])


def clean_isbn(raw):
    """Return the normalised ISBN ('' if blank) or raise RuleViolation if the format is invalid."""
    isbn = normalise_isbn(raw)
    if not isbn:
        return ""
    if len(isbn) == 10 and _isbn10_ok(isbn):
        return isbn
    if len(isbn) == 13 and _isbn13_ok(isbn):
        return isbn
    raise RuleViolation("isbn_invalid", "Enter a valid 10- or 13-digit ISBN.")


def _isbn13_check_digit(first12):
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(first12))
    return str((10 - total % 10) % 10)


def _isbn10_check_digit(first9):
    total = sum(int(ch) * (10 - i) for i, ch in enumerate(first9))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


def isbn_variants(isbn):
    """The same book can carry its ISBN-10 or ISBN-13 form: return every equivalent form."""
    variants = {isbn}
    if len(isbn) == 10:
        base = "978" + isbn[:9]
        variants.add(base + _isbn13_check_digit(base))
    elif len(isbn) == 13 and isbn.startswith("978"):
        variants.add(isbn[3:12] + _isbn10_check_digit(isbn[3:12]))
    return variants


def find_isbn_duplicates(isbn, exclude_pk=None):
    if not isbn:
        return BookEdition.objects.none()
    qs = BookEdition.objects.filter(isbn__in=isbn_variants(isbn))
    return qs.exclude(pk=exclude_pk) if exclude_pk else qs


# ---------- Categories ----------
def get_or_create_category(name, actor=None):
    name = (name or "").strip()
    existing = Category.objects.filter(name__iexact=name).first()
    if existing:
        return existing, False
    cat = Category.objects.create(name=name)
    record(
        "category.create",
        actor=actor,
        entity_type="category",
        entity_id=cat.pk,
        summary={"name": name},
    )
    return cat, True


# ---------- Editions ----------
def _check_year(year):
    if year is not None and not (1000 <= year <= timezone.localdate().year + 1):
        raise RuleViolation("year_invalid", "Enter a plausible publication year.")


@transaction.atomic
def create_edition(
    *,
    actor,
    title,
    category,
    author="",
    publisher="",
    edition_label="",
    publication_year=None,
    isbn="",
    description="",
    notes="",
    isbn_acknowledged=False,
):
    if not (title or "").strip():
        raise RuleViolation("title_required", "Title is required.")
    _check_year(publication_year)
    isbn = clean_isbn(isbn)
    if find_isbn_duplicates(isbn).exists() and not isbn_acknowledged:
        raise RuleViolation(
            "isbn_duplicate", "Another edition already has this ISBN. Confirm to continue anyway."
        )
    edition = BookEdition.objects.create(
        title=title.strip(),
        author=(author or "").strip(),
        publisher=publisher.strip(),
        edition_label=edition_label.strip(),
        publication_year=publication_year,
        category=category,
        isbn=isbn,
        description=description,
        notes=notes,
    )
    record(
        "edition.create",
        actor=actor,
        entity_type="edition",
        entity_id=edition.pk,
        summary={"title": edition.title},
    )
    return edition


@transaction.atomic
def update_edition(edition, *, actor, isbn_acknowledged=False, **fields):
    _check_year(fields.get("publication_year"))
    if "isbn" in fields:
        fields["isbn"] = clean_isbn(fields["isbn"])
        if (
            fields["isbn"] != edition.isbn
            and find_isbn_duplicates(fields["isbn"], exclude_pk=edition.pk).exists()
            and not isbn_acknowledged
        ):
            raise RuleViolation(
                "isbn_duplicate",
                "Another edition already has this ISBN. Confirm to continue anyway.",
            )
    changed = [f for f, v in fields.items() if getattr(edition, f) != v]
    for f, v in fields.items():
        setattr(edition, f, v)
    edition.save()
    record(
        "edition.update",
        actor=actor,
        entity_type="edition",
        entity_id=edition.pk,
        summary={"changed_fields": changed},
    )
    return edition


@transaction.atomic
def set_edition_archived(edition, archived, *, actor):
    edition.is_archived = archived
    edition.save(update_fields=["is_archived"])
    record(
        "edition.archive" if archived else "edition.unarchive",
        actor=actor,
        entity_type="edition",
        entity_id=edition.pk,
    )


# ---------- Copies ----------
def format_copy_code(number):
    return f"COV-C-{number:06d}"


def _log_event(copy, event_type, frm="", to="", note="", actor=None, event_date=None):
    return CopyEvent.objects.create(
        copy=copy,
        event_type=event_type,
        from_value=frm,
        to_value=to,
        note=note,
        actor=actor,
        event_date=event_date or timezone.localdate(),
    )


def _create_copy(edition, acquisition, code, *, actor, shelf_location="", notes="", legacy_ref=""):
    copy = BookCopy.objects.create(
        copy_code=code,
        barcode=code,
        edition=edition,
        acquisition=acquisition,
        shelf_location=shelf_location,
        notes=notes,
        legacy_ref=legacy_ref,
    )
    _log_event(
        copy, CopyEvent.Type.ACQUIRED, to=acquisition.source if acquisition else "", actor=actor
    )
    return copy


@transaction.atomic
def add_copies(
    edition,
    count,
    *,
    actor,
    source,
    donor_name="",
    donor_contact="",
    acquired_on=None,
    unit_cost_paise=None,
    shelf_location="",
    notes="",
    codes=None,
    legacy_refs=None,
):
    """Add `count` physical copies with one acquisition record.

    Codes are generated (COV-C-000001...) unless `codes` supplies existing legacy IDs.
    """
    if count < 1:
        raise RuleViolation("count_invalid", "Add at least one copy.")
    if source not in Acquisition.Source.values:
        raise RuleViolation("source_invalid", "Choose purchased or donated.")
    if codes is not None:
        if len(codes) != count:
            raise RuleViolation("codes_count", "Supply exactly one copy ID per copy.")
        clean = [c.strip() for c in codes]
        if len(set(clean)) != len(clean) or any(not c for c in clean):
            raise RuleViolation("code_duplicate", "Copy IDs must be unique and non-empty.")
        reserved = [c for c in clean if re.match(r"^COV-C-\d+$", c)]
        if reserved:
            raise RuleViolation(
                "code_reserved", f"Copy IDs in the format COV-C-nnnnnn are reserved: {reserved[0]}"
            )
        clash = BookCopy.objects.filter(copy_code__in=clean).values_list("copy_code", flat=True)
        if clash:
            raise RuleViolation("code_exists", f"Copy ID already exists: {', '.join(clash)}")
    acquisition = Acquisition.objects.create(
        edition=edition,
        source=source,
        donor_name=donor_name,
        donor_contact=donor_contact,
        acquired_on=acquired_on,
        unit_cost_paise=unit_cost_paise,
        notes="",
    )
    created = []
    for i in range(count):
        code = codes[i].strip() if codes is not None else format_copy_code(next_value("copy"))
        legacy = (legacy_refs[i] if legacy_refs else "") or ""
        created.append(
            _create_copy(
                edition,
                acquisition,
                code,
                actor=actor,
                shelf_location=shelf_location,
                notes=notes,
                legacy_ref=legacy,
            )
        )
    record(
        "copy.add",
        actor=actor,
        entity_type="edition",
        entity_id=edition.pk,
        summary={"count": count, "codes": [c.copy_code for c in created][:20]},
    )
    return created


ALLOWED_STATES = {
    BookCopy.Condition.GOOD: {BookCopy.Status.AVAILABLE, BookCopy.Status.WITHDRAWN},
    BookCopy.Condition.POOR: {BookCopy.Status.WITHDRAWN},
    BookCopy.Condition.DAMAGED: {BookCopy.Status.DAMAGED, BookCopy.Status.WITHDRAWN},
    BookCopy.Condition.LOST: {BookCopy.Status.LOST},
}


def validate_state(condition, status):
    """Rules linking condition and circulation status (B14, Q6)."""
    if status == BookCopy.Status.BORROWED:
        raise RuleViolation("borrowed_is_automatic", "Borrowed status is set only by circulation.")
    if condition not in ALLOWED_STATES or status not in ALLOWED_STATES[condition]:
        raise RuleViolation(
            "state_invalid",
            f"A {condition} copy cannot have status {status}. "
            "Poor copies are Withdrawn; Damaged copies are Damaged or Withdrawn; "
            "Lost copies are Lost; only Good copies can be Available.",
        )


@transaction.atomic
def set_copy_state(copy, *, condition, status, actor, note=""):
    copy = BookCopy.objects.select_for_update().get(pk=copy.pk)
    if copy.status == BookCopy.Status.BORROWED:
        raise RuleViolation("copy_borrowed", "A borrowed copy changes state when it is returned.")
    validate_state(condition, status)
    if (copy.condition, copy.status) == (condition, status):
        return copy
    if copy.condition != condition:
        _log_event(copy, CopyEvent.Type.CONDITION_CHANGED, copy.condition, condition, note, actor)
    if copy.status != status:
        _log_event(copy, CopyEvent.Type.STATUS_CHANGED, copy.status, status, note, actor)
    old = (copy.condition, copy.status)
    copy.condition, copy.status = condition, status
    copy.save(update_fields=["condition", "status"])
    record(
        "copy.state",
        actor=actor,
        entity_type="copy",
        entity_id=copy.copy_code,
        summary={"from": list(old), "to": [condition, status], "note": note[:200]},
    )
    return copy


def search_editions(query="", archived=False, category=None):
    from django.db.models import Q

    qs = BookEdition.objects.filter(is_archived=archived).select_related("category")
    query = clean_query(query)
    if query:
        isbn = normalise_isbn(query)
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(author__icontains=query)
            | Q(isbn=isbn)
            | Q(copies__copy_code__iexact=query)
            | Q(copies__barcode__iexact=query)
        ).distinct()
    if category:
        qs = qs.filter(category=category)
    return qs
