import json
from copy import deepcopy

from django.test import SimpleTestCase, TestCase

from audit.models import AuditEvent
from audit.redaction import MAX_DEPTH, REDACTED, is_sensitive_key, scrub, scrub_text
from audit.services import record

SENSITIVE_KEYS = [
    "password",
    "secret",
    "api_key",
    "otp",
    "recovery_code",
    "code_hash",
    "passphrase",
    "token",
    "reset_token",
    "authorization",
    "key",
    "session",
    # variants and casings
    "Password",
    "PASSWORD",
    "apiKey",
    "API-KEY",
    "resetToken",
    "access_token",
    "refresh_token",
    "csrf_token",
    "session_id",
    "sessionKey",
    "secret_note",
    "password_confirm",
    "new_password1",
    "client_secret",
    "private_key",
    "encryption_key",
    "Authorization",
    "recovery code",
]
BUSINESS_KEYS = [
    "title",
    "author",
    "username",
    "status",
    "reason",
    "note",
    "copy_code",
    "isbn",
    "entity_type",
    "changed_fields",
    "token_cycle",
    "token_amount_paise",
    "forfeited_token_amount",
    "dedupe_key",
    "key_fingerprint",
    "session_expiry",
    "sha256",
    "hash",
    "code",
    "from",
    "to",
    "rows",
    "mode",
]


class KeyClassificationTests(SimpleTestCase):
    def test_every_required_key_is_sensitive(self):
        for key in SENSITIVE_KEYS:
            self.assertTrue(is_sensitive_key(key), key)

    def test_business_keys_are_not_redacted(self):
        for key in BUSINESS_KEYS:
            self.assertFalse(is_sensitive_key(key), key)


class ScrubStructureTests(SimpleTestCase):
    def test_each_required_category_is_redacted(self):
        for key in SENSITIVE_KEYS:
            self.assertEqual(scrub({key: "value-123"}), {key: REDACTED}, key)

    def test_nested_dicts_lists_and_tuples(self):
        data = {
            "a": {"b": {"password": "x", "ok": "fine"}},
            "list": [{"api_key": "k"}, "plain", ({"otp": "123456"},)],
            "deep": [[{"token": "t"}]],
        }
        out = scrub(data)
        self.assertEqual(out["a"]["b"], {"password": REDACTED, "ok": "fine"})
        self.assertEqual(out["list"][0], {"api_key": REDACTED})
        self.assertEqual(out["list"][2], [{"otp": REDACTED}])
        self.assertEqual(out["deep"], [[{"token": REDACTED}]])
        self.assertNotIn("123456", json.dumps(out))

    def test_input_is_not_mutated_and_depth_is_bounded(self):
        data = {"password": "x", "n": [1, 2]}
        before = deepcopy(data)
        scrub(data)
        self.assertEqual(data, before)
        nested = current = {}
        for _ in range(MAX_DEPTH + 5):
            current["x"] = {}
            current = current["x"]
        self.assertIn("[truncated]", json.dumps(scrub(nested)))

    def test_scalars_and_unknown_objects(self):
        self.assertEqual(
            scrub({"n": 5, "f": 1.5, "b": True, "none": None}),
            {"n": 5, "f": 1.5, "b": True, "none": None},
        )
        from decimal import Decimal

        self.assertEqual(scrub({"amount": Decimal("10.50")}), {"amount": "10.50"})


class ScrubValueTests(SimpleTestCase):
    def test_secret_shaped_values_are_redacted(self):
        cases = {
            "Bearer abcdef0123456789": "Bearer " + REDACTED,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl": REDACTED,
            "password=hunter2 and more": "password=" + REDACTED + " and more",
            "token: abc123": "token: " + REDACTED,
            "api_key=sk_live_x": "api_key=" + REDACTED,
            "visit /accounts/reset/MQ/6k4-0123456789abcdef/ now": "visit /accounts/reset/"
            + REDACTED
            + " now",
            "code ABCDE-FGHJK used": "code " + REDACTED + " used",
            "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7": REDACTED,
        }
        for raw, expected in cases.items():
            self.assertEqual(scrub_text(raw), expected, raw)

    def test_ordinary_business_data_survives(self):
        keep = [
            "The Hobbit",
            "Asha Rao",
            "COV-M-000001",
            "COV-C-000042",
            "SUB-2027-000001",
            "9780261102217",
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "Requested by the member",
            "token cycle 2 started",
            "₹500 refundable token",
            "o@example.com",
            "2026-11-11",
            "Donated by Sam, shelf B-12",
            "Unknown author",
        ]
        for text in keep:
            self.assertEqual(scrub_text(text), text, text)
        self.assertEqual(
            scrub({"title": "The Hobbit", "token_cycle": 2}),
            {"title": "The Hobbit", "token_cycle": 2},
        )


class RecordTests(TestCase):
    def test_record_redacts_summary_entity_and_action(self):
        ev = record(
            "x password=oops",
            entity_type="t",
            entity_id="Bearer abcdefghijkl",
            summary={"note": "token=abc999", "password": "p", "n": {"secret": "s"}},
        )
        self.assertNotIn("oops", ev.action)
        self.assertNotIn("abcdefghijkl", ev.entity_id)
        text = json.dumps(ev.summary)
        for leaked in ("abc999", '"p"', '"s"'):
            self.assertNotIn(leaked, text)

    def test_stored_row_contains_no_plaintext(self):
        record("a", summary={"reset_token": "RAWTOKEN1234", "authorization": "Bearer ZZZZZZZZZZZZ"})
        raw = str(list(AuditEvent.objects.values())[0])
        self.assertNotIn("RAWTOKEN1234", raw)
        self.assertNotIn("ZZZZZZZZZZZZ", raw)
