"""Excel import: stage -> validate -> preview -> owner confirms -> atomic execution."""

import csv
import hashlib
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from openpyxl import Workbook, load_workbook

from audit.services import record
from core.errors import RuleViolation

from . import services
from .models import Acquisition, BookCopy, Category, ImportBatch, ImportRow

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
MAX_COPIES_PER_ROW = 100
SHEET = "Books"
COLUMNS = [
    "Title",
    "Author",
    "ISBN",
    "Publisher",
    "Edition",
    "Publication Year",
    "Category",
    "Description",
    "Notes",
    "Acquisition Source",
    "Donor Name",
    "Acquired On",
    "Unit Cost (INR)",
    "Copies",
    "Legacy Copy IDs",
    "Shelf Location",
]
REQUIRED_COLUMNS = ["Title", "Category"]
DOC = {
    "Title": "Required.",
    "Author": "Optional. Leave blank or write 'Unknown author'.",
    "ISBN": "Optional. ISBN-10 or ISBN-13; duplicates produce a warning.",
    "Publisher": "Optional.",
    "Edition": "Optional edition information, e.g. '2nd'.",
    "Publication Year": "Optional four-digit year.",
    "Category": "Required. Must exist, or choose 'create missing categories' during import.",
    "Description": "Optional.",
    "Notes": "Optional.",
    "Acquisition Source": "purchased or donated (default purchased).",
    "Donor Name": "Optional; for donated books.",
    "Acquired On": "Optional date (YYYY-MM-DD).",
    "Unit Cost (INR)": "Optional amount in rupees.",
    "Copies": "Number of physical copies (default 1, max 100).",
    "Legacy Copy IDs": (
        "Optional. Comma-separated; one per copy. Required when 'use supplied copy IDs' is chosen."
    ),
    "Shelf Location": "Optional.",
}


def build_template():
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(COLUMNS)
    guide = wb.create_sheet("Instructions")
    guide.append(["Column", "Notes"])
    for col in COLUMNS:
        guide.append([col, DOC[col]])
    guide.append([])
    guide.append(
        ["Example row", "The Books sheet is empty on purpose; nothing in this sheet is imported."]
    )
    guide.append(
        [
            "Example",
            "The Hobbit | Tolkien | 9780261102217 | HarperCollins | 2nd | 1999 | Fiction",
        ]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- reading ----------
def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value).strip()


def read_workbook(upload):
    """Validate the upload and return (sha256, rows) where rows = [(row_number, {column: text})]."""
    name = getattr(upload, "name", "") or ""
    if not name.lower().endswith(".xlsx"):
        raise RuleViolation(
            "file_type", "Upload an Excel .xlsx file (macro or legacy formats are not accepted)."
        )
    content = upload.read()
    if len(content) > MAX_BYTES:
        raise RuleViolation("file_size", "The file is larger than 5 MB.")
    if not content.startswith(b"PK") or not zipfile.is_zipfile(io.BytesIO(content)):
        raise RuleViolation("file_type", "The file is not a valid .xlsx workbook.")
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise RuleViolation("file_type", "The workbook could not be read.") from None
    if SHEET not in wb.sheetnames:
        raise RuleViolation("sheet_missing", f"The workbook must contain a sheet named '{SHEET}'.")
    ws = wb[SHEET]
    iterator = ws.iter_rows(values_only=True)
    header = next(iterator, None)
    if not header:
        raise RuleViolation("header_missing", "The Books sheet is empty.")
    names = [_text(h) for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        raise RuleViolation("header_missing", f"Missing required column(s): {', '.join(missing)}.")
    rows = []
    for offset, values in enumerate(iterator, start=2):
        record_ = {n: _text(v) for n, v in zip(names, values, strict=False) if n}
        if not any(record_.values()):
            continue
        rows.append((offset, record_))
        if len(rows) > MAX_ROWS:
            raise RuleViolation("too_many_rows", f"The file has more than {MAX_ROWS} data rows.")
    if not rows:
        raise RuleViolation("no_rows", "The Books sheet has no data rows.")
    return hashlib.sha256(content).hexdigest(), rows


# ---------- validation ----------
def _issue(field, message, code):
    return {"field": field, "message": message, "code": code}


def _split_ids(text):
    return [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]


def validate_row(raw, batch, category_names):
    """Return (clean, errors, warnings) for one raw row under the batch's owner choices."""
    errors, warnings = [], []
    clean = {}
    title = raw.get("Title", "")
    if not title:
        errors.append(_issue("Title", "Title is required.", "title_required"))
    clean["title"] = title
    clean["author"] = raw.get("Author", "")
    for key, col in (
        ("publisher", "Publisher"),
        ("edition_label", "Edition"),
        ("description", "Description"),
        ("notes", "Notes"),
        ("donor_name", "Donor Name"),
        ("shelf_location", "Shelf Location"),
    ):
        clean[key] = raw.get(col, "")
    # year
    year_text = raw.get("Publication Year", "")
    clean["publication_year"] = None
    if year_text:
        try:
            year = int(float(year_text))
            services._check_year(year)
            clean["publication_year"] = year
        except (ValueError, RuleViolation):
            errors.append(
                _issue("Publication Year", "Enter a plausible four-digit year.", "year_invalid")
            )
    # isbn
    try:
        clean["isbn"] = services.clean_isbn(raw.get("ISBN", ""))
    except RuleViolation as exc:
        clean["isbn"] = ""
        errors.append(_issue("ISBN", exc.message, exc.code))
    # category
    category = raw.get("Category", "")
    clean["category"] = category
    if not category:
        errors.append(_issue("Category", "Category is required.", "category_required"))
    elif category.lower() not in category_names:
        if batch.create_missing_categories:
            warnings.append(
                _issue("Category", f"Category '{category}' will be created.", "category_new")
            )
        else:
            errors.append(
                _issue(
                    "Category",
                    f"Unknown category '{category}'. Row rejected "
                    "(choose 'create missing categories' to allow it).",
                    "category_unknown",
                )
            )
    # acquisition
    source = (raw.get("Acquisition Source", "") or "purchased").lower()
    if source not in Acquisition.Source.values:
        errors.append(
            _issue("Acquisition Source", "Use 'purchased' or 'donated'.", "source_invalid")
        )
    clean["source"] = source
    acquired = raw.get("Acquired On", "")
    clean["acquired_on"] = None
    if acquired:
        try:
            clean["acquired_on"] = date.fromisoformat(acquired[:10]).isoformat()
        except ValueError:
            errors.append(_issue("Acquired On", "Use the date format YYYY-MM-DD.", "date_invalid"))
    cost = raw.get("Unit Cost (INR)", "")
    clean["unit_cost_paise"] = None
    if cost:
        try:
            value = Decimal(cost)
            if value < 0:
                raise InvalidOperation
            clean["unit_cost_paise"] = int(value * 100)
        except InvalidOperation:
            errors.append(_issue("Unit Cost (INR)", "Enter a non-negative amount.", "cost_invalid"))
    # copies
    copies_text = raw.get("Copies", "") or "1"
    try:
        copies = int(float(copies_text))
        if not 1 <= copies <= MAX_COPIES_PER_ROW:
            raise ValueError
    except ValueError:
        copies = 0
        errors.append(
            _issue(
                "Copies",
                f"Copies must be a whole number from 1 to {MAX_COPIES_PER_ROW}.",
                "copies_invalid",
            )
        )
    clean["copies"] = copies
    ids = _split_ids(raw.get("Legacy Copy IDs", ""))
    clean["legacy_ids"] = ids
    if batch.copy_id_mode == ImportBatch.CopyIdMode.SUPPLIED:
        if copies and len(ids) != copies:
            errors.append(
                _issue("Legacy Copy IDs", "Supply exactly one copy ID per copy.", "ids_count")
            )
    elif ids and copies and len(ids) != copies:
        errors.append(
            _issue("Legacy Copy IDs", "Supply one reference per copy or leave blank.", "ids_count")
        )
    if len(set(ids)) != len(ids):
        errors.append(_issue("Legacy Copy IDs", "Copy IDs repeat within the row.", "ids_duplicate"))
    # ISBN duplicates in database (warning)
    if clean["isbn"] and services.find_isbn_duplicates(clean["isbn"]).exists():
        warnings.append(
            _issue("ISBN", "An existing edition already has this ISBN.", "isbn_duplicate")
        )
    return clean, errors, warnings


def _cross_row_checks(rows, batch):
    """File-wide and database conflicts that need to see every row."""
    seen_isbn, seen_ids, seen_key = {}, {}, {}
    existing_codes = set(BookCopy.objects.values_list("copy_code", flat=True))
    existing_legacy = set(
        BookCopy.objects.exclude(legacy_ref="").values_list("legacy_ref", flat=True)
    )
    for row in rows:
        c = row.clean
        isbn = c.get("isbn")
        if isbn:
            if isbn in seen_isbn:
                row.warnings.append(
                    _issue("ISBN", f"Same ISBN as row {seen_isbn[isbn]}.", "isbn_duplicate")
                )
            seen_isbn.setdefault(isbn, row.row_number)
        key = (
            c.get("title", "").lower(),
            c.get("author", "").lower(),
            c.get("edition_label", "").lower(),
            c.get("publication_year"),
            c.get("publisher", "").lower(),
        )
        if c.get("title"):
            if key in seen_key:
                row.warnings.append(
                    _issue(
                        "Title", f"Looks like a duplicate of row {seen_key[key]}.", "row_duplicate"
                    )
                )
            seen_key.setdefault(key, row.row_number)
        for cid in c.get("legacy_ids", []):
            if cid in seen_ids:
                row.errors.append(
                    _issue(
                        "Legacy Copy IDs",
                        f"Copy ID '{cid}' repeats row {seen_ids[cid]}.",
                        "id_repeated",
                    )
                )
            seen_ids.setdefault(cid, row.row_number)
            if batch.copy_id_mode == ImportBatch.CopyIdMode.SUPPLIED and cid in existing_codes:
                row.errors.append(
                    _issue("Legacy Copy IDs", f"Copy ID '{cid}' already exists.", "id_exists")
                )
            if cid in existing_legacy:
                row.errors.append(
                    _issue(
                        "Legacy Copy IDs",
                        f"Legacy reference '{cid}' is already recorded.",
                        "id_exists",
                    )
                )


def validate_batch(batch):
    """(Re)compute validation for every staged row under the batch's current owner choices."""
    category_names = {n.lower() for n in Category.objects.values_list("name", flat=True)}
    rows = list(batch.rows.all())
    for row in rows:
        row.clean, row.errors, row.warnings = validate_row(row.data, batch, category_names)
    _cross_row_checks(rows, batch)
    for row in rows:
        row.is_valid = not row.errors
    ImportRow.objects.bulk_update(rows, ["clean", "errors", "warnings", "is_valid"])
    return rows


def stage_import(upload, *, actor):
    sha, rows = read_workbook(upload)
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            filename=getattr(upload, "name", "")[:255], sha256=sha, created_by=actor
        )
        ImportRow.objects.bulk_create(
            [ImportRow(batch=batch, row_number=n, data=data) for n, data in rows]
        )
        validate_batch(batch)
        record(
            "import.stage",
            actor=actor,
            entity_type="import",
            entity_id=batch.pk,
            summary={"filename": batch.filename, "rows": len(rows)},
        )
    return batch


def set_options(batch, *, create_missing_categories, copy_id_mode, isbn_warnings_acknowledged):
    if batch.status != ImportBatch.Status.VALIDATED:
        raise RuleViolation("batch_closed", "This import is already closed.")
    if copy_id_mode not in ImportBatch.CopyIdMode.values:
        raise RuleViolation("mode_invalid", "Choose how copy IDs are handled.")
    batch.create_missing_categories = bool(create_missing_categories)
    batch.copy_id_mode = copy_id_mode
    batch.isbn_warnings_acknowledged = bool(isbn_warnings_acknowledged)
    batch.save()
    return validate_batch(batch)


def summarise(batch):
    rows = list(batch.rows.all())
    valid = [r for r in rows if r.is_valid]
    return {
        "total": len(rows),
        "valid": len(valid),
        "invalid": len(rows) - len(valid),
        "warning_rows": sum(1 for r in valid if r.warnings),
        "isbn_warning_rows": sum(
            1 for r in rows if any(w["code"] == "isbn_duplicate" for w in r.warnings)
        ),
        "copies": sum(r.clean.get("copies", 0) for r in valid),
        "new_categories": sorted(
            {
                r.clean["category"]
                for r in valid
                if any(w["code"] == "category_new" for w in r.warnings)
            }
        ),
    }


@transaction.atomic
def execute_import(batch, *, actor, accept_excluded=False):
    batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status != ImportBatch.Status.VALIDATED:
        raise RuleViolation("batch_closed", "This import was already confirmed or closed.")
    before = summarise(batch)
    rows = validate_batch(batch)  # re-check against the live catalogue
    if summarise(batch)["valid"] != before["valid"]:
        raise RuleViolation("stale_preview", "The catalogue changed. Review the preview again.")
    summary = summarise(batch)
    if summary["valid"] == 0:
        raise RuleViolation("nothing_to_import", "No valid rows to import.")
    if summary["isbn_warning_rows"] and not batch.isbn_warnings_acknowledged:
        raise RuleViolation(
            "isbn_ack_required", "Acknowledge the duplicate ISBN warnings to continue."
        )
    if summary["invalid"] and not accept_excluded:
        raise RuleViolation(
            "excluded_ack_required",
            f"{summary['invalid']} row(s) with errors will be left out. "
            "Confirm to import the valid rows.",
        )
    created_editions = created_copies = created_categories = 0
    for row in rows:
        if not row.is_valid:
            continue
        c = row.clean
        category, created = services.get_or_create_category(c["category"], actor=actor)
        created_categories += int(created)
        edition = services.create_edition(
            actor=actor,
            title=c["title"],
            category=category,
            author=c["author"],
            publisher=c["publisher"],
            edition_label=c["edition_label"],
            publication_year=c["publication_year"],
            isbn=c["isbn"],
            description=c["description"],
            notes=c["notes"],
            isbn_acknowledged=True,
        )
        supplied = batch.copy_id_mode == ImportBatch.CopyIdMode.SUPPLIED
        ids = c["legacy_ids"]
        copies = services.add_copies(
            edition,
            c["copies"],
            actor=actor,
            source=c["source"],
            donor_name=c["donor_name"],
            acquired_on=date.fromisoformat(c["acquired_on"]) if c["acquired_on"] else None,
            unit_cost_paise=c["unit_cost_paise"],
            shelf_location=c["shelf_location"],
            codes=ids if supplied else None,
            legacy_refs=None if supplied else (ids if ids else None),
        )
        created_editions += 1
        created_copies += len(copies)
    batch.status = ImportBatch.Status.CONFIRMED
    batch.summary = {
        "editions": created_editions,
        "copies": created_copies,
        "categories_created": created_categories,
        "rows_skipped": summary["invalid"],
        "rows_total": summary["total"],
    }
    batch.save()
    record(
        "import.confirm",
        actor=actor,
        entity_type="import",
        entity_id=batch.pk,
        summary=batch.summary,
    )
    return batch


def _csv_safe(value):
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def error_report_csv(batch):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Row", "Severity", "Field", "Message", "Code"])
    for row in batch.rows.all():
        for severity, items in (("error", row.errors), ("warning", row.warnings)):
            for item in items:
                writer.writerow(
                    [
                        row.row_number,
                        severity,
                        _csv_safe(item["field"]),
                        _csv_safe(item["message"]),
                        item["code"],
                    ]
                )
    return out.getvalue()
