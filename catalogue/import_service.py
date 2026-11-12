"""Excel import: stage -> validate -> preview -> owner confirms -> atomic execution."""

import csv
import hashlib
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import DatabaseError, transaction
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from audit.services import record
from core.errors import RuleViolation
from core.money import MAX_PAISE

from . import services
from .models import Acquisition, BookCopy, BookEdition, Category, ImportBatch, ImportRow

MAX_BYTES = 5 * 1024 * 1024  # compressed upload size
MAX_UNCOMPRESSED = 50 * 1024 * 1024  # total decompressed size of all workbook parts
MAX_ZIP_ENTRIES = 500
MAX_ROWS = 2000  # data rows
MAX_SCAN_ROWS = 10000  # physical rows read (stops runaway empty/formatted rows)
MAX_COLS = 60
MAX_COPIES_PER_ROW = 100
MAX_LONG_TEXT = 5000  # Description / Notes are unbounded in the database; capped defensively
MAX_COST_PAISE = MAX_PAISE  # shared 64-bit-backed limit (core/money.py)
GENERATED_CODE = re.compile(r"^COV-C-\d+$")  # reserved for generated copy IDs
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _limit(model, field):
    return model._meta.get_field(field).max_length


# Limits come straight from the model fields so preview and database can never disagree.
TEXT_LIMITS = {
    "Title": _limit(BookEdition, "title"),
    "Author": _limit(BookEdition, "author"),
    "Publisher": _limit(BookEdition, "publisher"),
    "Edition": _limit(BookEdition, "edition_label"),
    "Category": _limit(Category, "name"),
    "Donor Name": _limit(Acquisition, "donor_name"),
    "Shelf Location": _limit(BookCopy, "shelf_location"),
}
COPY_CODE_MAX = min(_limit(BookCopy, "copy_code"), _limit(BookCopy, "barcode"))
LEGACY_REF_MAX = _limit(BookCopy, "legacy_ref")
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
    return str(value).replace("\x00", "").strip()


def read_workbook(upload):
    """Validate the upload and return (sha256, rows) where rows = [(row_number, {column: text})].

    Processing is bounded: compressed size, decompressed size, zip entry count, scanned rows,
    data rows and columns are all capped, and workbooks carrying macros are refused.
    """
    name = getattr(upload, "name", "") or ""
    if not name.lower().endswith(".xlsx"):
        raise RuleViolation(
            "file_type", "Upload an Excel .xlsx file (macro or legacy formats are not accepted)."
        )
    size = getattr(upload, "size", None)
    if size is not None and size > MAX_BYTES:
        raise RuleViolation("file_size", "The file is larger than 5 MB.")
    content = upload.read(MAX_BYTES + 1)  # never read more than the limit into memory
    if len(content) > MAX_BYTES:
        raise RuleViolation("file_size", "The file is larger than 5 MB.")
    if not content.startswith(b"PK") or not zipfile.is_zipfile(io.BytesIO(content)):
        raise RuleViolation("file_type", "The file is not a valid .xlsx workbook.")
    _check_zip_bounds(content)
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise RuleViolation("file_type", "The workbook could not be read.") from None
    try:
        if SHEET not in wb.sheetnames:
            raise RuleViolation(
                "sheet_missing", f"The workbook must contain a sheet named '{SHEET}'."
            )
        iterator = wb[SHEET].iter_rows(values_only=True, max_col=MAX_COLS)
        header = next(iterator, None)
        if not header:
            raise RuleViolation("header_missing", "The Books sheet is empty.")
        names = [_text(h) for h in header]
        missing = [c for c in REQUIRED_COLUMNS if c not in names]
        if missing:
            raise RuleViolation(
                "header_missing", f"Missing required column(s): {', '.join(missing)}."
            )
        rows = []
        for scanned, values in enumerate(iterator, start=2):
            if scanned > MAX_SCAN_ROWS:
                raise RuleViolation(
                    "too_many_rows", f"The sheet has more than {MAX_SCAN_ROWS} rows."
                )
            record_ = {n: _text(v) for n, v in zip(names, values, strict=False) if n}
            if not any(record_.values()):
                continue
            rows.append((scanned, record_))
            if len(rows) > MAX_ROWS:
                raise RuleViolation(
                    "too_many_rows", f"The file has more than {MAX_ROWS} data rows."
                )
    finally:
        wb.close()
    if not rows:
        raise RuleViolation("no_rows", "The Books sheet has no data rows.")
    return hashlib.sha256(content).hexdigest(), rows


def _check_zip_bounds(content):
    """Reject decompression bombs and macro-carrying workbooks before openpyxl sees them."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        raise RuleViolation("file_type", "The file is not a valid .xlsx workbook.") from None
    if len(infos) > MAX_ZIP_ENTRIES:
        raise RuleViolation("file_complex", "The workbook has too many internal parts.")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise RuleViolation("file_complex", "The workbook expands to an unreasonable size.")
    for info in infos:
        if info.file_size > 1_000_000 and info.file_size / max(1, info.compress_size) > 200:
            raise RuleViolation("file_complex", "The workbook has a suspicious compression ratio.")
        if info.filename.lower().endswith("vbaproject.bin"):
            raise RuleViolation("file_type", "Workbooks containing macros are not accepted.")


# ---------- validation ----------
def _issue(field, message, code):
    return {"field": field, "message": message, "code": code}


def _split_ids(text):
    return [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]


def _finite_decimal(text, field, errors, code):
    """Parse a finite, sanely-sized number or record an error and return None."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        errors.append(_issue(field, "Enter a number.", code))
        return None
    if not value.is_finite() or abs(value.adjusted()) > 12:
        errors.append(_issue(field, "Enter a finite, reasonably sized number.", code))
        return None
    return value


def _whole_number(text, field, errors, code, low, high):
    value = _finite_decimal(text, field, errors, code)
    if value is None:
        return None
    if value != value.to_integral_value():
        errors.append(_issue(field, "Enter a whole number.", code))
        return None
    number = int(value)
    if not low <= number <= high:
        errors.append(_issue(field, f"Enter a number from {low} to {high}.", code))
        return None
    return number


def _length_check(raw, clean, column, key, errors, limit=None):
    value = raw.get(column, "")
    limit = limit or TEXT_LIMITS[column]
    if len(value) > limit:
        errors.append(
            _issue(column, f"Too long ({len(value)} characters; maximum {limit}).", "too_long")
        )
    if CONTROL_CHARS.search(value):
        errors.append(_issue(column, "Contains control characters.", "control_chars"))
    clean[key] = value


def validate_row(raw, batch, category_names):
    """Return (clean, errors, warnings) for one raw row under the batch's owner choices.

    Never raises: anything unexpected becomes a row error so the preview and the confirmation
    step always agree (a row is only 'valid' if execution cannot fail for a predictable reason).
    """
    errors, warnings, clean = [], [], {}
    try:
        _validate_row(raw, batch, category_names, clean, errors, warnings)
    except Exception:  # defensive: report instead of crashing the whole upload
        errors.append(_issue("Row", "This row could not be processed.", "row_unprocessable"))
    return clean, errors, warnings


def _validate_row(raw, batch, category_names, clean, errors, warnings):
    _length_check(raw, clean, "Title", "title", errors)
    if not clean["title"]:
        errors.append(_issue("Title", "Title is required.", "title_required"))
    _length_check(raw, clean, "Author", "author", errors)
    _length_check(raw, clean, "Publisher", "publisher", errors)
    _length_check(raw, clean, "Edition", "edition_label", errors)
    _length_check(raw, clean, "Donor Name", "donor_name", errors)
    _length_check(raw, clean, "Shelf Location", "shelf_location", errors)
    for column, key in (("Description", "description"), ("Notes", "notes")):
        value = raw.get(column, "")
        if len(value) > MAX_LONG_TEXT:
            errors.append(
                _issue(column, f"Too long (maximum {MAX_LONG_TEXT} characters).", "too_long")
            )
        clean[key] = value
    # publication year
    clean["publication_year"] = None
    if raw.get("Publication Year", ""):
        low, high = 1000, timezone.localdate().year + 1
        clean["publication_year"] = _whole_number(
            raw["Publication Year"], "Publication Year", errors, "year_invalid", low, high
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
    elif len(category) > TEXT_LIMITS["Category"]:
        errors.append(
            _issue("Category", f"Too long (maximum {TEXT_LIMITS['Category']}).", "too_long")
        )
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
    clean["acquired_on"] = None
    if raw.get("Acquired On", ""):
        try:
            clean["acquired_on"] = date.fromisoformat(raw["Acquired On"][:10]).isoformat()
        except ValueError:
            errors.append(_issue("Acquired On", "Use the date format YYYY-MM-DD.", "date_invalid"))
    clean["unit_cost_paise"] = None
    if raw.get("Unit Cost (INR)", ""):
        cost = _finite_decimal(raw["Unit Cost (INR)"], "Unit Cost (INR)", errors, "cost_invalid")
        if cost is not None:
            if cost < 0:
                errors.append(
                    _issue("Unit Cost (INR)", "Enter a non-negative amount.", "cost_invalid")
                )
            elif cost != cost.quantize(Decimal("0.01")):
                errors.append(
                    _issue("Unit Cost (INR)", "Use at most two decimal places.", "cost_invalid")
                )
            elif cost * 100 > MAX_COST_PAISE:
                errors.append(_issue("Unit Cost (INR)", "Amount is too large.", "cost_invalid"))
            else:
                clean["unit_cost_paise"] = int(cost * 100)
    # copies
    copies = _whole_number(
        raw.get("Copies", "") or "1", "Copies", errors, "copies_invalid", 1, MAX_COPIES_PER_ROW
    )
    clean["copies"] = copies or 0
    ids = _split_ids(raw.get("Legacy Copy IDs", ""))
    clean["legacy_ids"] = ids
    supplied = batch.copy_id_mode == ImportBatch.CopyIdMode.SUPPLIED
    if copies:
        if supplied and len(ids) != copies:
            errors.append(
                _issue("Legacy Copy IDs", "Supply exactly one copy ID per copy.", "ids_count")
            )
        elif not supplied and ids and len(ids) != copies:
            errors.append(
                _issue(
                    "Legacy Copy IDs", "Supply one reference per copy or leave blank.", "ids_count"
                )
            )
    if len(set(ids)) != len(ids):
        errors.append(_issue("Legacy Copy IDs", "Copy IDs repeat within the row.", "ids_duplicate"))
    for cid in ids:
        limit = COPY_CODE_MAX if supplied else LEGACY_REF_MAX
        if len(cid) > limit:
            errors.append(
                _issue(
                    "Legacy Copy IDs", f"'{cid[:20]}...' is too long (maximum {limit}).", "too_long"
                )
            )
        if CONTROL_CHARS.search(cid):
            errors.append(
                _issue(
                    "Legacy Copy IDs",
                    "Copy IDs cannot contain control characters.",
                    "control_chars",
                )
            )
        if supplied and not (cid.isascii() and cid.isprintable()):
            errors.append(
                _issue(
                    "Legacy Copy IDs",
                    f"'{cid[:20]}' must use printable ASCII (barcode requirement).",
                    "id_charset",
                )
            )
        if supplied and GENERATED_CODE.match(cid):
            errors.append(
                _issue(
                    "Legacy Copy IDs",
                    f"'{cid}' uses the format reserved for generated copy IDs.",
                    "id_reserved",
                )
            )
    # ISBN duplicates in database (warning)
    if clean["isbn"] and services.find_isbn_duplicates(clean["isbn"]).exists():
        warnings.append(
            _issue("ISBN", "An existing edition already has this ISBN.", "isbn_duplicate")
        )


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
    try:
        return _run_import(batch, rows, summary, actor)
    except DatabaseError as exc:  # unforeseen: roll everything back and report cleanly
        raise RuleViolation(
            "import_failed",
            "The import could not be completed and nothing was changed. "
            "Please review the file and try again.",
        ) from exc


def _run_import(batch, rows, summary, actor):
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
    """Neutralise spreadsheet formulas, including ones hidden behind tabs, CR/LF or whitespace."""
    text = str(value)
    stripped = text.lstrip(" \t\r\n\u00a0\ufeff\u200b")
    if text[:1] in ("\t", "\r", "\n") or stripped[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


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
