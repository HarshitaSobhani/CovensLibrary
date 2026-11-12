"""Phase 4a: loan constraints and triggers, exercised with raw SQL (as the owner role)."""

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from circulation import services
from circulation.models import Loan

from .helpers import D, make_copies, make_owner, member_at


def execute(sql, params=None):
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(sql, params or [])


class LoanConstraintTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = member_at(self.owner)
        self.other = member_at(self.owner, "Bo", "9876543211")
        self.copies = make_copies(self.owner, 3)
        self.loan = services.issue_loan(
            actor=self.owner, member=self.member, copy=self.copies[0], today=D(2026, 12, 1)
        )

    def insert(self, member=None, copy=None, **over):
        values = dict(
            member_id=(member or self.other).pk,
            copy_id=(copy or self.copies[1]).pk,
            issued_on="2026-12-02",
            due_on="2027-01-01",
            issued_processed_at="now()",
            issue_condition="good",
            return_condition="",
            status="active",
            backdated=False,
            backdate_reason="",
            backdate_confirmed=False,
            return_backdated=False,
            return_backdate_reason="",
            notes="",
            issued_by_id=self.owner.pk,
        )
        values.update(over)
        cols = ", ".join(values)
        marks = ", ".join("now()" if v == "now()" else "%s" for v in values.values())
        params = [v for v in values.values() if v != "now()"]
        execute(f"INSERT INTO circulation_loan ({cols}) VALUES ({marks})", params)

    def test_a_second_active_loan_for_the_same_member_or_copy_is_impossible(self):
        with self.assertRaises(IntegrityError):
            self.insert(member=self.member)
        with self.assertRaises(IntegrityError):
            self.insert(copy=self.copies[0])
        self.insert()  # a different member and copy is fine
        self.assertEqual(Loan.objects.filter(status="active").count(), 2)

    def test_invalid_rows_are_rejected(self):
        bad = {
            "due before issue": dict(due_on="2026-12-01", issued_on="2026-12-02"),
            "issued in poor condition": dict(issue_condition="poor"),
            "unknown status": dict(status="vanished"),
            "active with a return date": dict(returned_on="2026-12-05"),
            "returned without a date": dict(status="returned", return_condition="good"),
            "returned with an inconsistent condition": dict(
                status="returned",
                return_condition="lost",
                returned_on="2026-12-05",
                returned_processed_at="now()",
            ),
            "lost with a good condition": dict(
                status="lost",
                return_condition="good",
                returned_on="2026-12-05",
                returned_processed_at="now()",
            ),
            "returned before issued": dict(
                status="returned",
                return_condition="good",
                returned_on="2026-12-01",
                returned_processed_at="now()",
            ),
            "backdated without a reason": dict(backdated=True),
            "backdated reason without confirmation": dict(backdated=True, backdate_reason="r"),
            "return backdating without a reason": dict(return_backdated=True),
        }
        for name, over in bad.items():
            with self.subTest(name), self.assertRaises(IntegrityError):
                self.insert(**over)

    def test_loans_are_never_deleted_and_issue_facts_never_change(self):
        with self.assertRaisesMessage(DatabaseError, "never deleted"):
            execute("DELETE FROM circulation_loan")
        for column, value in (
            ("issued_on", "'2026-11-20'"),
            ("member_id", str(self.other.pk)),
            ("copy_id", str(self.copies[2].pk)),
            ("issue_condition", "'poor'"),
            ("backdated", "true"),
            ("backdate_reason", "'x'"),
        ):
            with self.subTest(column), self.assertRaises(DatabaseError):
                execute(f"UPDATE circulation_loan SET {column} = {value} WHERE id = {self.loan.pk}")

    def test_a_due_date_can_only_move_forward_while_the_loan_is_active(self):
        execute("UPDATE circulation_loan SET due_on = due_on + 30 WHERE id = %s", [self.loan.pk])
        with self.assertRaisesMessage(DatabaseError, "only move forward"):
            execute("UPDATE circulation_loan SET due_on = due_on - 1 WHERE id = %s", [self.loan.pk])

    def test_a_closed_loan_is_frozen(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2026, 12, 5))
        for sql in (
            "notes = 'edited'",
            "due_on = due_on + 1",
            "status = 'active'",
            "returned_on = '2026-12-02'",
            "return_condition = 'poor'",
        ):
            with (
                self.subTest(sql),
                self.assertRaisesMessage(DatabaseError, "closed loan cannot change"),
            ):
                execute(f"UPDATE circulation_loan SET {sql} WHERE id = {self.loan.pk}")

    def test_the_loan_cannot_be_reopened_to_hide_a_return(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2026, 12, 5))
        with self.assertRaises(DatabaseError):
            execute(
                "UPDATE circulation_loan SET status='active', returned_on=NULL, "
                "returned_processed_at=NULL, return_condition='' WHERE id = %s",
                [self.loan.pk],
            )

    def test_renewal_rows_are_append_only_and_constrained(self):
        def renewal(old="2026-12-31", new="2027-01-30", on="2026-12-20"):
            execute(
                "INSERT INTO circulation_loanrenewal (loan_id, renewed_on, renewed_at, old_due_on, "
                "new_due_on, notes, renewed_by_id) VALUES (%s, %s, now(), %s, %s, '', %s)",
                [self.loan.pk, on, old, new, self.owner.pk],
            )

        renewal()
        with self.assertRaises(IntegrityError):  # same new due date twice
            renewal()
        with self.assertRaises(IntegrityError):  # must extend
            renewal(old="2027-01-30", new="2027-01-30")
        with self.assertRaises(IntegrityError):  # never after the due date
            renewal(old="2026-12-31", new="2027-02-01", on="2027-01-01")
        for sql in (
            "UPDATE circulation_loanrenewal SET notes = 'x'",
            "DELETE FROM circulation_loanrenewal",
        ):
            with self.subTest(sql), self.assertRaisesMessage(DatabaseError, "append-only"):
                execute(sql)
