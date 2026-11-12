"""The transactional outbox (decision D-23): storage, atomicity, deduplication, privacy,
immutability, and the removal of the old in-memory mechanism."""

import ast
import json
import threading
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core import outbox
from core.models import DomainEvent

ROOT = Path(settings.BASE_DIR)


class EmitTests(TestCase):
    def emit(self, key="k:1", **kw):
        return outbox.emit(outbox.LOAN_ISSUED, dedupe_key=key, **kw)

    def test_an_event_is_stored_with_ids_dates_and_a_unique_event_id(self):
        self.assertTrue(
            self.emit(member_id=7, payload={"loan_id": 1}, event_date=date(2026, 12, 1))
        )
        event = DomainEvent.objects.get()
        self.assertEqual(
            (event.event_type, event.member_id, event.payload), ("loan.issued", 7, {"loan_id": 1})
        )
        self.assertEqual(event.event_date, date(2026, 12, 1))
        self.assertIsNotNone(event.event_id)
        self.assertIsNotNone(event.created_at.tzinfo)

    def test_the_same_dedupe_key_never_creates_a_second_event(self):
        self.assertTrue(self.emit())
        self.assertFalse(self.emit())
        self.assertEqual(DomainEvent.objects.count(), 1)
        self.assertEqual(DomainEvent.objects.get().member_id, None)  # the first one is kept

    def test_the_database_itself_enforces_uniqueness_and_non_empty_keys(self):
        self.emit()
        sql = (
            "INSERT INTO core_domainevent (event_id, dedupe_key, event_type, payload, event_date, "
            "occurred_at, created_at) VALUES (gen_random_uuid(), %s, %s, '{}', current_date, "
            "now(), now())"
        )
        for key, kind in (("k:1", "loan.issued"), ("", "loan.issued"), ("k:2", "")):
            with self.subTest(key=key, kind=kind), self.assertRaises(IntegrityError):
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(sql, [key, kind])

    def test_invalid_emission_is_refused(self):
        for key in ("", "x" * 201):
            with self.assertRaises(ValueError):
                self.emit(key)
        with self.assertRaises(ValueError):
            outbox.emit("loan.vanished", dedupe_key="k")
        self.assertEqual(DomainEvent.objects.count(), 0)

    def test_payloads_carry_ids_and_codes_never_personal_or_secret_fields(self):
        for key in (
            "email",
            "full_name",
            "name",
            "mobile",
            "password",
            "reason",
            "notes",
            "session",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.emit(f"k:{key}", payload={key: "x"})
        with self.assertRaises(ValueError):
            self.emit("k:nested", payload={"outer": {"email": "a@b.c"}})
        self.assertEqual(DomainEvent.objects.count(), 0)

    def test_timestamps_business_date_and_processing_time_are_kept_apart(self):
        past = timezone.now() - timedelta(days=3)
        self.emit(event_date=date(2026, 12, 18), occurred_at=past)
        event = DomainEvent.objects.get()
        self.assertEqual((event.event_date, event.occurred_at), (date(2026, 12, 18), past))
        self.assertGreaterEqual(event.created_at, event.occurred_at)

    def test_the_default_business_date_is_the_kolkata_calendar_date(self):
        self.emit("k:today")
        self.assertEqual(DomainEvent.objects.get().event_date, timezone.localdate())
        self.assertEqual(str(timezone.get_current_timezone()), "Asia/Kolkata")


class ImmutabilityTests(TestCase):
    def setUp(self):
        outbox.emit(outbox.LOAN_ISSUED, dedupe_key="k:1", member_id=1)

    def execute(self, sql):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql)

    def test_events_can_be_neither_updated_nor_deleted_even_by_the_owner_role(self):
        for sql in (
            "UPDATE core_domainevent SET event_type = 'loan.returned'",
            "UPDATE core_domainevent SET payload = '{}'",
            "DELETE FROM core_domainevent",
        ):
            with self.subTest(sql), self.assertRaisesMessage(DatabaseError, "append-only"):
                self.execute(sql)
        self.assertEqual(DomainEvent.objects.count(), 1)

    def test_the_trigger_function_pins_its_search_path(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT p.proconfig, pg_get_userbyid(p.proowner) FROM pg_trigger t "
                "JOIN pg_proc p ON p.oid = t.tgfoid WHERE t.tgname = 'core_domainevent_append_only'"
            )
            config, owner = cur.fetchone()
        self.assertEqual(config, ["search_path=pg_catalog"])
        self.assertEqual(owner, settings.DATABASES["default"]["USER"])

    def test_emitting_outside_a_transaction_is_refused(self):
        from unittest import mock

        with mock.patch.object(connection, "in_atomic_block", False):
            with self.assertRaises(RuntimeError):
                outbox.emit(outbox.LOAN_ISSUED, dedupe_key="k:2")


class OldMechanismIsGoneTests(TestCase):
    """Regression: durable delivery must never rely on the removed in-memory callbacks."""

    def source_files(self):
        for app in (
            "accounts",
            "audit",
            "catalogue",
            "circulation",
            "core",
            "finance",
            "members",
            "memberships",
        ):
            for path in (ROOT / app).rglob("*.py"):
                if "tests" in path.parts or "migrations" in path.parts:
                    continue
                yield path

    def test_the_in_memory_events_module_no_longer_exists(self):
        with self.assertRaises(ImportError):
            __import__("circulation.events")
        self.assertFalse((ROOT / "circulation" / "events.py").exists())

    def test_no_application_code_uses_on_commit_callbacks_or_signals_for_events(self):
        for path in self.source_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "on_commit":
                    self.fail(f"on_commit used in {path}")
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Signal":
                    self.fail(f"a custom Signal is defined in {path}")
                if isinstance(node, ast.Name) and node.id == "domain_event":
                    self.fail(f"domain_event referenced in {path}")

    def test_events_exist_immediately_without_any_commit_hook_or_receiver(self):
        outbox.emit(outbox.LOAN_ISSUED, dedupe_key="k:immediate")
        self.assertEqual(DomainEvent.objects.count(), 1)  # no captureOnCommitCallbacks needed

    def test_every_event_type_has_a_producer_in_the_application(self):
        text = " ".join(p.read_text() for p in self.source_files() if p.name != "outbox.py")
        for name in (
            "MEMBER_REGISTERED",
            "SUBSCRIPTION_ACTIVATED",
            "SUBSCRIPTION_RENEWED",
            "PAYMENT_RECEIPTED",
            "TOKEN_COLLECTED",
            "TOKEN_REFUNDED",
            "TOKEN_FORFEITED",
            "LOAN_ISSUED",
            "LOAN_RENEWED",
            "LOAN_RETURNED",
            "LOAN_LOST",
        ):
            self.assertIn(f"outbox.{name}", text, name)

    def test_the_payload_is_valid_json(self):
        outbox.emit(outbox.LOAN_ISSUED, dedupe_key="k:j", payload={"a": [1, 2], "b": {"c": "d"}})
        json.dumps(DomainEvent.objects.get().payload)


class DedupeConflictTests(TestCase):
    def emit(self, event_type=outbox.LOAN_ISSUED, key="k:1", member_id=5, payload=None):
        return outbox.emit(
            event_type,
            dedupe_key=key,
            member_id=member_id,
            payload={"a": 1, "b": {"c": 2}} if payload is None else payload,
        )

    def test_an_identical_duplicate_returns_false_and_adds_no_row(self):
        self.assertTrue(self.emit())
        self.assertFalse(self.emit())
        self.assertEqual(DomainEvent.objects.count(), 1)

    def test_json_key_order_does_not_matter(self):
        self.assertTrue(self.emit(payload={"a": 1, "b": {"c": 2, "d": 3}}))
        self.assertFalse(self.emit(payload={"b": {"d": 3, "c": 2}, "a": 1}))
        self.assertEqual(DomainEvent.objects.count(), 1)

    def test_a_different_payload_raises_and_the_original_is_untouched(self):
        self.emit()
        original = DomainEvent.objects.values().get()
        for payload in ({"a": 2, "b": {"c": 2}}, {"a": 1}, {"a": 1, "b": {"c": 2}, "x": 0}):
            with self.assertRaises(outbox.OutboxKeyConflict):
                self.emit(payload=payload)
        self.assertEqual(list(DomainEvent.objects.values()), [original])

    def test_a_different_event_type_or_member_raises(self):
        self.emit()
        with self.assertRaises(outbox.OutboxKeyConflict):
            self.emit(event_type=outbox.LOAN_RETURNED)
        with self.assertRaises(outbox.OutboxKeyConflict):
            self.emit(member_id=6)
        with self.assertRaises(outbox.OutboxKeyConflict):
            self.emit(member_id=None)
        self.assertEqual(DomainEvent.objects.count(), 1)

    def test_a_mismatch_rolls_back_the_whole_transaction(self):
        self.emit(key="keep")
        with self.assertRaises(outbox.OutboxKeyConflict):
            with transaction.atomic():
                self.emit(key="new:1")  # business work in the same transaction
                self.emit(key="keep", payload={"changed": True})
        self.assertEqual(list(DomainEvent.objects.values_list("dedupe_key", flat=True)), ["keep"])

    def test_the_conflict_is_a_documented_specific_exception(self):
        self.assertTrue(outbox.OutboxKeyConflict.__doc__)
        self.assertFalse(issubclass(outbox.OutboxKeyConflict, (ValueError, IntegrityError)))
        self.assertIn("OutboxKeyConflict", outbox.emit.__doc__)


class DedupeRaceTests(TransactionTestCase):
    def race(self, payloads):
        barrier = threading.Barrier(len(payloads))
        results = []

        def work(payload):
            try:
                barrier.wait()
                with transaction.atomic():
                    results.append(
                        outbox.emit(
                            outbox.LOAN_ISSUED, dedupe_key="race:1", member_id=1, payload=payload
                        )
                    )
            except outbox.OutboxKeyConflict:
                results.append("conflict")
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_concurrent_identical_emits_store_exactly_one_event(self):
        results = self.race([{"a": 1}, {"a": 1}, {"a": 1}])
        self.assertEqual(sorted(map(str, results)), ["False", "False", "True"])
        self.assertEqual(DomainEvent.objects.filter(dedupe_key="race:1").count(), 1)

    def test_concurrent_different_payloads_store_one_and_reject_the_other(self):
        results = self.race([{"a": 1}, {"a": 2}])
        self.assertEqual(sorted(map(str, results)), ["True", "conflict"])
        self.assertEqual(DomainEvent.objects.filter(dedupe_key="race:1").count(), 1)
