"""Phase 6b: member statement and the full ZIP export."""

import csv
import hashlib
import io
import json
import zipfile

from django.apps import apps
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from audit.models import AuditEvent
from circulation import forfeiture, services
from circulation.tests.helpers import D, make_owner, overdue_loan
from communications import services as comms
from communications.models import Notification
from communications.tests.helpers import give_email, ist
from members import services as members
from members.models import Member
from memberships.tests.helpers import PW, register
from reports import export, statement

NAME = "Zainab Qureshi"
ADDRESS = "zq@example.com"


def read_zip(response_or_bytes):
    data = getattr(response_or_bytes, "content", response_or_bytes)
    archive = zipfile.ZipFile(io.BytesIO(data))
    return archive, {n: archive.read(n) for n in archive.namelist()}


def csv_rows(raw):
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


class StatementTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.member, copy, self.loan = overdue_loan(self.owner, name=NAME, number="9811100077")
        forfeiture.process_overdue_forfeitures(today=D(2027, 1, 9))

    def build(self, member=None, start=None, end=None):
        return statement.build(member or self.member, start, end, D(2027, 1, 9))

    def test_it_collects_subscription_money_tokens_and_loans(self):
        report = self.build()
        titles = [s.title for s in report.sections]
        self.assertEqual(titles[0], "Member")
        subs = next(s for s in report.sections if s.title == "Subscriptions")
        self.assertEqual(subs.rows[0][5], 80000)
        money = next(s for s in report.sections if s.title.startswith("Payments"))
        kinds = [r[1] for r in money.rows]
        self.assertEqual(kinds, ["Subscription payment", "Token collection", "Token forfeiture"])
        self.assertEqual(money.rows[-1][5], "automatic (day 8)")
        ledger = next(s for s in report.sections if s.title.startswith("Token ledger"))
        self.assertEqual([r[3] for r in ledger.rows], [50000, -50000])
        loans = next(s for s in report.sections if s.title == "Loans")
        self.assertEqual(loans.rows[0][1], "B-9811100077")

    def test_a_date_range_limits_money_ledger_and_loans(self):
        report = self.build(start=D(2027, 1, 1), end=D(2027, 1, 31))
        money = next(s for s in report.sections if s.title.startswith("Payments"))
        self.assertEqual([r[1] for r in money.rows], ["Token forfeiture"])
        loans = next(s for s in report.sections if s.title == "Loans")
        self.assertEqual(loans.rows, [])
        self.assertIn("01 Jan 2027 to 31 Jan 2027", report.subtitle)

    def test_other_members_never_appear(self):
        other = register(
            self.owner, name="Other Person", number="9811100078", today=D(2026, 11, 11)
        ).member
        text = " ".join(str(c) for s in self.build(other).sections for r in s.rows for c in r)
        self.assertNotIn("B-9811100077", text)
        self.assertNotIn(NAME, text)

    def test_an_anonymised_member_shows_only_the_id(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2027, 1, 10))
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        member = Member.objects.get(pk=self.member.pk)
        text = " ".join(str(c) for s in self.build(member).sections for r in s.rows for c in r)
        self.assertIn("Anonymised member", text)
        self.assertIn("COV-M-000001", text)
        for needle in (NAME, "+919811100077"):
            self.assertNotIn(needle, text)

    def test_views_formats_lookup_and_errors(self):
        url = reverse("reports:statement", args=[self.member.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        for fmt, magic in (("pdf", b"%PDF"), ("csv", b"\xef\xbb\xbf"), ("xlsx", b"PK")):
            response = self.client.get(url + f"?format={fmt}")
            self.assertEqual(response.status_code, 200, fmt)
            self.assertTrue(response.content.startswith(magic), fmt)
            self.assertIn("coven-statement-COV-M-000001", response["Content-Disposition"])
        self.assertEqual(self.client.get(url + "?format=exe").status_code, 404)
        self.assertEqual(
            self.client.get(reverse("reports:statement", args=[9999])).status_code, 404
        )
        bad = self.client.get(url + "?start=2027-02-01&end=2027-01-01&format=pdf")
        self.assertContains(bad, "valid dates")
        self.assertNotIn("attachment", bad.get("Content-Disposition", ""))
        found = self.client.get(reverse("reports:statement_lookup") + "?member_no=cov-m-000001")
        self.assertRedirects(found, url, fetch_redirect_response=False)
        missed = self.client.get(
            reverse("reports:statement_lookup") + "?member_no=COV-M-999999", follow=True
        )
        self.assertContains(missed, "No member has this ID")
        events = AuditEvent.objects.filter(action="report.export", entity_id="statement")
        self.assertEqual(events.count(), 3)
        self.assertNotIn(NAME, str(events.first().summary))

    def test_login_is_required(self):
        self.client.logout()
        for url in (
            reverse("reports:statement", args=[self.member.pk]),
            reverse("reports:statement", args=[self.member.pk]) + "?format=pdf",
            reverse("reports:statement_lookup"),
        ):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_the_member_page_links_the_statement(self):
        page = self.client.get(reverse("members:detail", args=[self.member.pk])).content.decode()
        self.assertIn(reverse("reports:statement", args=[self.member.pk]), page)


class FullExportTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.member, copy, self.loan = overdue_loan(self.owner, name=NAME, number="9811100077")
        give_email(self.member, ADDRESS)
        comms.run_cycle(ist(2027, 1, 3, 10))  # real emails with subjects, bodies and addresses
        forfeiture.process_overdue_forfeitures(today=D(2027, 1, 9))
        comms.run_cycle(ist(2027, 1, 9, 10))

    def zip(self):
        return read_zip(self.client.post(reverse("reports:export")))

    def test_the_archive_has_a_csv_per_dataset_one_workbook_and_a_manifest(self):
        archive, files = self.zip()
        expected = {f"{name}.csv" for name, *_ in export.DATASETS}
        self.assertEqual(set(files), expected | {"coven_export.xlsx", "manifest.json"})
        self.assertIsNone(archive.testzip())

    def test_the_manifest_matches_the_files_and_the_database(self):
        archive, files = self.zip()
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["export_version"], 1)
        self.assertEqual(manifest["timezone"], "Asia/Kolkata")
        by_name = {f["name"]: f for f in manifest["files"]}
        self.assertEqual(set(by_name), set(files) - {"manifest.json"})
        for name, entry in by_name.items():
            self.assertEqual(entry["sha256"], hashlib.sha256(files[name]).hexdigest(), name)
        for name, queryset, *_ in export.DATASETS:
            # the export's own audit event is written after the data was read
            now = queryset.count() - (1 if name == "audit_events" else 0)
            self.assertEqual(by_name[f"{name}.csv"]["rows"], now, name)
            self.assertEqual(len(csv_rows(files[f"{name}.csv"])) - 1, now, name)

    def test_the_workbook_has_a_sheet_per_dataset_with_the_same_row_counts(self):
        archive, files = self.zip()
        wb = load_workbook(io.BytesIO(files["coven_export.xlsx"]))
        self.assertEqual(wb.sheetnames, [name[:31] for name, *_ in export.DATASETS])
        for name, queryset, *_ in export.DATASETS:
            now = queryset.count() - (1 if name == "audit_events" else 0)
            self.assertEqual(wb[name[:31]].max_row - 1, now, name)

    def test_no_message_text_addresses_or_audit_summaries_are_exported(self):
        self.assertTrue(
            Notification.objects.exclude(body="").exists()
        )  # they exist in the database
        archive, files = self.zip()
        others = {n: v for n, v in files.items() if n != "members.csv"}
        blob = b"\n".join(others.values()).decode("utf-8", "ignore")
        self.assertNotIn(ADDRESS, blob)  # the member's own email is master data in members.csv only
        self.assertIn(ADDRESS, files["members.csv"].decode("utf-8-sig"))
        self.assertNotIn("Dear ", blob)  # no email body
        self.assertNotIn("Your Coven", blob)  # no email subject
        header = csv_rows(files["notifications.csv"])[0]
        self.assertEqual(
            header,
            [
                "dedupe_key",
                "kind",
                "member__membership_no",
                "status",
                "skip_code",
                "attempt_count",
                "created_at",
                "accepted_at",
                "scheduled_for",
            ],
        )
        self.assertEqual(csv_rows(files["audit_events.csv"])[0][-1], "entity_id")

    def test_amounts_are_exact_integer_paise(self):
        archive, files = self.zip()
        rows = csv_rows(files["financial_transactions.csv"])
        header, body = rows[0], rows[1:]
        amount = header.index("amount_paise")
        self.assertEqual(sorted(int(r[amount]) for r in body), [50000, 50000, 80000])
        entries = csv_rows(files["payment_entries.csv"])
        self.assertTrue(all(r[2].isdigit() for r in entries[1:]))

    def test_anonymised_members_are_exported_as_anonymised(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2027, 1, 10))
        members.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        archive, files = self.zip()
        text = files["members.csv"].decode("utf-8-sig")
        self.assertIn("Anonymised member", text)
        for needle in (NAME, "+919811100077", ADDRESS):
            self.assertNotIn(needle, b"\n".join(files.values()).decode("utf-8", "ignore"))

    def test_formula_injection_is_escaped_in_csv_and_xlsx(self):
        Member.objects.filter(pk=self.member.pk).update(
            full_name="=1+1", mobile_e164="+919811100077"
        )
        archive, files = self.zip()
        rows = csv_rows(files["members.csv"])
        self.assertEqual(rows[1][1], "'=1+1")
        wb = load_workbook(io.BytesIO(files["coven_export.xlsx"]))
        cell = wb["members"].cell(row=2, column=2)
        self.assertEqual((cell.value, cell.data_type), ("'=1+1", "s"))

    def test_access_rules_and_audit(self):
        url = reverse("reports:export")
        self.assertEqual(self.client.get(url).status_code, 405)  # POST only
        response = self.client.post(url)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertRegex(response["Content-Disposition"], r"coven-library-export-\d{8}-\d{6}\.zip")
        event = AuditEvent.objects.get(action="report.full_export")
        self.assertGreater(event.summary["rows"], 0)
        self.assertNotIn(NAME, str(event.summary))
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(AuditEvent.objects.filter(action="report.full_export").count(), 1)

    def test_csrf_is_enforced_for_the_export(self):
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.login(username="owner", password=PW)
        self.assertEqual(strict.post(reverse("reports:export")).status_code, 403)

    def test_every_model_is_exported_or_explicitly_excluded(self):
        exported = {qs.model._meta.label for _, qs, *__ in export.DATASETS}
        mine = {
            "members",
            "catalogue",
            "finance",
            "memberships",
            "circulation",
            "communications",
            "audit",
            "core",
            "accounts",
        }
        every = {m._meta.label for m in apps.get_models() if m._meta.app_label in mine}
        self.assertEqual(every - exported - set(export.NOT_EXPORTED), set())
        self.assertEqual(exported & set(export.NOT_EXPORTED), set())

    def test_an_empty_database_still_exports(self):

        Member.objects.none()  # an unused queryset must not matter
        data, manifest = export.build_zip()
        self.assertTrue(data.startswith(b"PK"))
        self.assertGreater(len(manifest["files"]), 10)


class FullExportOnEmptyDataTests(TestCase):
    def test_empty_library(self):
        make_owner()
        data, manifest = export.build_zip()
        archive, files = read_zip(data)
        self.assertIsNone(archive.testzip())
        self.assertEqual(csv_rows(files["members.csv"]), [csv_rows(files["members.csv"])[0]])
