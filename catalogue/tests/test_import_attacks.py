"""Hostile spreadsheets: XML entity attacks, malformed archives and path tricks in the import."""

import io
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from catalogue import import_service as svc
from catalogue.models import ImportBatch
from core.errors import RuleViolation

OWNER_PW = "Correct-Horse-9!"


def tamper(mutate):
    """The official template with one part of the workbook rewritten by ``mutate(name, data)``."""
    source = zipfile.ZipFile(io.BytesIO(svc.build_template()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            target.writestr(info.filename, mutate(info.filename, data))
    return out.getvalue()


class HostileWorkbookTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password=OWNER_PW)
        self.client.login(username="owner", password=OWNER_PW)

    def upload(self, content, name="books.xlsx"):
        return self.client.post(
            reverse("catalogue:import_start"),
            {"file": SimpleUploadedFile(name, content)},
            follow=True,
        )

    def test_an_external_entity_never_reads_a_local_file(self):
        def mutate(name, data):
            if name == "xl/sharedStrings.xml":
                text = data.decode()
                bomb = '<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
                text = text.replace("<sst", bomb + "<sst", 1).replace("<t>", "<t>&leak;", 1)
                return text.encode()
            return data

        response = self.upload(tamper(mutate))
        for batch in ImportBatch.objects.all():
            self.assertNotIn("root:", str(batch.__dict__))
        self.assertNotContains(response, "root:x:0")
        self.assertNotIn(b"root:x:0", response.content)

    def test_an_entity_expansion_bomb_is_contained(self):
        laughs = "".join(
            f'<!ENTITY l{i} "{"&l%d;" % (i - 1) * 10 if i else "ha"}">' for i in range(0, 9)
        )

        def mutate(name, data):
            if name == "xl/sharedStrings.xml":
                text = data.decode().replace("<sst", f"<!DOCTYPE x [{laughs}]><sst", 1)
                return text.replace("<t>", "<t>&l8;", 1).encode()
            return data

        response = self.upload(tamper(mutate))  # must return promptly, not exhaust memory
        self.assertIn(response.status_code, (200,))
        self.assertLess(len(response.content), 5 * 1024 * 1024)

    def test_non_workbooks_and_disguised_files_are_refused_without_storing_them(self):
        before = ImportBatch.objects.count()
        for name, content in (
            ("a.xlsx", b"MZ\x90\x00 not a workbook"),
            ("a.xlsx", b"PK\x03\x04 truncated"),
            ("../../etc/cron.d/x.xlsx", b"PK"),
            ("a.xlsx", b"%PDF-1.7 ..."),
            ("a.xlsx", b"<html><script>alert(1)</script></html>"),
            ("a.xlsx", b""),
        ):
            response = self.upload(content, name)
            self.assertEqual(response.status_code, 200, name)
        self.assertEqual(ImportBatch.objects.count(), before)

    def test_a_workbook_with_a_traversal_member_name_is_inert(self):
        out = io.BytesIO(svc.build_template())
        with zipfile.ZipFile(out, "a") as z:
            z.writestr("../../../tmp/zz_pwned.txt", "pwned")
        self.upload(out.getvalue())
        import os

        self.assertFalse(os.path.exists("/tmp/zz_pwned.txt"))

    def test_service_limits_hold_for_a_direct_call(self):
        with self.assertRaises(RuleViolation):
            svc.read_workbook(SimpleUploadedFile("big.xlsx", b"PK" + b"0" * (6 * 1024 * 1024)))
