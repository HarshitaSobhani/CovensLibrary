from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from audit.models import AuditEvent
from audit.services import record, scrub


class AuditTests(TestCase):
    def test_record_writes_event(self):
        user = get_user_model().objects.create_user("o", password="pw-Strong-123!")
        ev = record(
            "member.create", actor=user, entity_type="member", entity_id=5, summary={"a": 1}
        )
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertEqual((ev.actor, ev.entity_id, ev.summary), (user, "5", {"a": 1}))

    def test_secrets_are_redacted(self):
        out = scrub({"password": "x", "nested": {"api_key": "k", "ok": 1}, "list": [{"otp": "1"}]})
        self.assertEqual(out["password"], "[redacted]")
        self.assertEqual(out["nested"], {"api_key": "[redacted]", "ok": 1})
        self.assertEqual(out["list"], [{"otp": "[redacted]"}])

    def test_events_are_append_only(self):
        ev = record("x")
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(pk=ev.pk).update(action="changed")
        with self.assertRaises(DatabaseError), transaction.atomic():
            ev.delete()
        with connection.cursor() as cur:
            with self.assertRaises(DatabaseError), transaction.atomic():
                cur.execute("DELETE FROM audit_auditevent")
        self.assertEqual(AuditEvent.objects.get(pk=ev.pk).action, "x")
