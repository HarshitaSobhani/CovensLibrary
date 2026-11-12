"""Full export (PRD 6.11, B41, decision D-28d): a ZIP with one CSV per entity, one Excel workbook
(a sheet per entity) and a manifest. All business records; NO message subjects, bodies, addresses
or error text, no audit summaries, no receipt snapshots. Amounts are exact integer paise.
Anonymised members are exported as anonymised. Text is escaped against formula injection."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime
from uuid import UUID

from django.utils import timezone
from openpyxl import Workbook

from audit.models import AuditEvent
from catalogue.models import Acquisition, BookCopy, BookEdition, Category, CopyEvent
from circulation.models import Loan, LoanRenewal, PendingForfeiture
from communications.models import Notification
from core.models import DomainEvent, SettingChangeEvent
from finance.models import (
    FinancialTransaction,
    PaymentEntry,
    ReceiptDocument,
    TokenCycle,
    TokenLedgerEntry,
)
from members.models import Member
from memberships.models import MemberSubscription, SubscriptionPlan

from .masking import mask
from .render import safe_text, xlsx_text

EXPORT_VERSION = 1

DATASETS = [
    (
        "members",
        Member.objects.order_by("id"),
        "membership_no full_name mobile_e164 email joined_on status needs_new_subscription "
        "anonymised_at created_at",
    ),
    (
        "subscription_plans",
        SubscriptionPlan.objects.order_by("id"),
        "code name duration_months price_paise is_active",
    ),
    (
        "subscriptions",
        MemberSubscription.objects.order_by("id"),
        "member__membership_no plan_name_snapshot duration_months_snapshot price_paise_snapshot "
        "starts_on expires_on cancelled_at token_cycle__cycle_no created_at",
    ),
    (
        "token_cycles",
        TokenCycle.objects.order_by("id"),
        "account__member__membership_no cycle_no amount_snapshot_paise started_on",
    ),
    (
        "token_ledger",
        TokenLedgerEntry.objects.order_by("id"),
        "cycle__account__member__membership_no cycle__cycle_no entry_type delta_paise "
        "forfeiture_trigger owner_confirmed effective_date processed_at transaction__receipt_no "
        "reason",
        ("cycle__account__member__anonymised_at", ["reason"]),
    ),
    (
        "financial_transactions",
        FinancialTransaction.objects.order_by("id"),
        "internal_txn_id member__membership_no kind cash_flow amount_paise effective_date "
        "processed_at receipt_no receipt_issued_on reverses__internal_txn_id "
        "adjusts__internal_txn_id backdated recorded_by__username recorded_by_system reason "
        "backdate_reason",
        ("member__anonymised_at", ["reason", "backdate_reason"]),
    ),
    (
        "payment_entries",
        PaymentEntry.objects.order_by("id"),
        "transaction__internal_txn_id method amount_paise paid_on reference recorded_at",
        ("transaction__member__anonymised_at", ["reference"]),
    ),
    (
        "receipt_documents",
        ReceiptDocument.objects.order_by("id"),
        "member__membership_no kind document_date issued_at",
    ),
    ("categories", Category.objects.order_by("id"), "name is_active"),
    (
        "editions",
        BookEdition.objects.order_by("id"),
        "title author publisher edition_label publication_year category__name isbn is_archived "
        "created_at",
    ),
    (
        "acquisitions",
        Acquisition.objects.order_by("id"),
        "edition__title source acquired_on unit_cost_paise",
    ),
    (
        "copies",
        BookCopy.objects.order_by("id"),
        "copy_code barcode edition__title condition status shelf_location created_at",
    ),
    (
        "copy_events",
        CopyEvent.objects.order_by("id"),
        "copy__copy_code event_type from_value to_value event_date processed_at loan_id note",
        ("loan__member__anonymised_at", ["note"]),
    ),
    (
        "loans",
        Loan.objects.order_by("id"),
        "member__membership_no copy__copy_code issued_on due_on returned_on status "
        "issue_condition return_condition backdated return_backdated issued_processed_at "
        "returned_processed_at",
    ),
    (
        "loan_renewals",
        LoanRenewal.objects.order_by("id"),
        "loan_id renewed_on old_due_on new_due_on renewed_at",
    ),
    (
        "pending_forfeitures",
        PendingForfeiture.objects.order_by("id"),
        "loan_id status created_at resolved_at resolved_by__username decline_reason confirm_reason",
        ("loan__member__anonymised_at", ["decline_reason", "confirm_reason"]),
    ),
    (
        "setting_changes",
        SettingChangeEvent.objects.order_by("id"),
        "setting_name old_value new_value actor__username changed_at",
    ),
    (
        "domain_events",
        DomainEvent.objects.order_by("id"),
        "event_id dedupe_key event_type member_id event_date occurred_at payload",
    ),
    (
        "notifications",
        Notification.objects.order_by("id"),
        "dedupe_key kind member__membership_no status skip_code attempt_count created_at "
        "accepted_at scheduled_for",
    ),
    (
        "audit_events",
        AuditEvent.objects.order_by("id"),
        "occurred_at actor__username action entity_type entity_id",
    ),
]
# Every model must be exported above or named here with the reason (a test enforces it).
NOT_EXPORTED = {
    "accounts.User": "owner credentials",
    "accounts.LoginEvent": "login attempts and addresses",
    "accounts.RecoveryCode": "hashed recovery codes",
    "core.LibrarySetting": "configuration (not records)",
    "core.IdSequence": "internal counters",
    "catalogue.LabelLayout": "printer layouts (configuration)",
    "catalogue.ImportBatch": "temporary import staging",
    "catalogue.ImportRow": "temporary import staging",
    "finance.TokenAccount": "no data beyond the member link (see token_cycles)",
    "communications.NotificationAttempt": "attempt log holds only times and error text",
    "communications.MessageTemplate": "WhatsApp wording (configuration)",
}
EXCLUDED = [
    "acquisition donor names and contacts (stored, but not displayed in the application and "
    "not part of any general export)",
    "notification subjects, bodies, recipient addresses and error text",
    "audit event summaries and receipt document snapshots",
    "owner credentials, sessions, recovery codes and login events",
]


def cell(value):
    """A plain, exactly-representable cell: ints stay ints, everything else is text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return timezone.localtime(value).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def collect():
    """[(name, columns, rows)] with every cell normalised."""
    out = []
    for name, queryset, fields, *masking in DATASETS:
        columns = fields.split()
        if masking:  # member-linked free text is masked for anonymised members (D-28i)
            flag_path, masked = masking[0]
            positions = [columns.index(c) for c in masked]
            rows = []
            for *values, flag in queryset.values_list(*columns, flag_path):
                for i in positions:
                    values[i] = mask(flag, values[i])
                rows.append([cell(v) for v in values])
        else:
            rows = [[cell(v) for v in row] for row in queryset.values_list(*columns)]
        out.append((name, columns, rows))
    return out


def _escaped(row):
    return [safe_text(v) if isinstance(v, str) else v for v in row]


def _csv_bytes(columns, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(_escaped(row))
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _xlsx_bytes(datasets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, columns, rows in datasets:
        ws = wb.create_sheet(name[:31])
        ws.append(columns)
        for row in rows:
            ws.append([xlsx_text(v) if isinstance(v, str) else v for v in row])
            for c in ws[ws.max_row]:
                if isinstance(c.value, str):
                    c.data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_zip(now=None):
    """(zip bytes, manifest dict). Generated on request and not stored."""
    now = now or timezone.now()
    datasets = collect()
    files, manifest_files = {}, []
    for name, columns, rows in datasets:
        files[f"{name}.csv"] = _csv_bytes(columns, rows)
        manifest_files.append((f"{name}.csv", len(rows)))
    files["coven_export.xlsx"] = _xlsx_bytes(datasets)
    manifest_files.append(("coven_export.xlsx", sum(len(r) for _, _, r in datasets)))
    manifest = {
        "application": "Coven's Library",
        "export_version": EXPORT_VERSION,
        "generated_at": timezone.localtime(now).isoformat(timespec="seconds"),
        "timezone": "Asia/Kolkata",
        "amounts": "integer paise (100 paise = Rs 1); never rounded",
        "excluded": EXCLUDED,
        "files": [
            {"name": n, "rows": rows, "sha256": hashlib.sha256(files[n]).hexdigest()}
            for n, rows in manifest_files
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return buf.getvalue(), manifest
