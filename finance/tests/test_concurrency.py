"""Real concurrent PostgreSQL connections: receipt numbers, idempotency and member locking."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from core.errors import RuleViolation
from finance import services as fs
from finance.models import FinancialTransaction
from members.models import Member

from .helpers import make_member, make_owner, sub_item

TODAY = date(2026, 11, 11)


def run_parallel(fn, n):
    def wrapped(i):
        try:
            return fn(i)
        except Exception as exc:  # collected and inspected by the test
            return exc
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(wrapped, range(n)))


class ConcurrentLedgerTests(TransactionTestCase):
    def setUp(self):
        self.owner = make_owner()
        self.members = [
            make_member(self.owner, f"Member {i}", f"98765432{i:02d}") for i in range(12)
        ]

    def tearDown(self):
        close_old_connections()

    def test_parallel_receipts_are_unique_and_gap_free(self):
        def work(i):
            with transaction.atomic():
                member = Member.objects.select_for_update().get(pk=self.members[i].pk)
                return (
                    fs.post_items(member=member, items=[sub_item()], actor=self.owner, today=TODAY)
                    .transactions[0]
                    .receipt_no
                )

        results = run_parallel(work, 12)
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        expected = [f"SUB-2026-{n:06d}" for n in range(1, 13)]
        self.assertEqual(sorted(results), expected)
        self.assertEqual(
            sorted(FinancialTransaction.objects.values_list("receipt_no", flat=True)), expected
        )

    def test_identical_double_submission_posts_exactly_once(self):
        key = uuid.uuid4()
        member_pk = self.members[0].pk

        def work(i):
            with transaction.atomic():
                member = Member.objects.select_for_update().get(pk=member_pk)  # serialises
                return fs.post_items(
                    member=member,
                    items=[sub_item(request_key=key)],
                    actor=self.owner,
                    today=TODAY,
                )

        results = run_parallel(work, 8)
        ok = [r for r in results if not isinstance(r, Exception)]
        dup = [r for r in results if isinstance(r, RuleViolation)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(dup), 7)
        self.assertTrue(all(r.code == "duplicate_submission" for r in dup))
        self.assertEqual(FinancialTransaction.objects.filter(request_key=key).count(), 1)

    def test_deferred_payment_sum_trigger_holds_at_real_commit(self):
        with connection.cursor() as cur:
            with self.assertRaisesMessage(Exception, "must equal the transaction amount"):
                with transaction.atomic():
                    cur.execute(
                        "INSERT INTO finance_receiptdocument "
                        "(member_id, kind, document_date, snapshot, issued_at) "
                        "VALUES (%s, 'subscription', '2026-11-11', '{}', now())",
                        [self.members[0].pk],
                    )
                    cur.execute(
                        "INSERT INTO finance_financialtransaction "
                        "(internal_txn_id, member_id, kind, cash_flow, amount_paise, "
                        "effective_date, processed_at, receipt_sequence, receipt_issued_on, "
                        "receipt_year, receipt_no, receipt_document_id, reason, backdated, "
                        "backdate_reason, backdate_financial_confirmed, recorded_by_id, "
                        "recorded_by_system) "
                        "SELECT gen_random_uuid(), %s, 'subscription_payment', 'in', 100, "
                        "'2026-11-11', now(), 'subscription', '2026-11-11', 2026, "
                        "'SUB-2026-000777', max(id), '', false, '', false, %s, '' "
                        "FROM finance_receiptdocument",
                        [self.members[0].pk, self.owner.pk],
                    )
            # the failed commit left nothing behind
        self.assertEqual(FinancialTransaction.objects.count(), 0)
