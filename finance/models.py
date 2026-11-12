"""Immutable financial ledger. Every table here is APPEND_ONLY (see core/dbsecurity.py):
database triggers reject UPDATE/DELETE and the runtime role holds only SELECT and INSERT.
Corrections are reversals or adjustments, never edits."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.money import MAX_PAISE


class ReceiptDocument(models.Model):
    """A frozen receipt snapshot; a combined registration printout lists both receipts."""

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="receipts")
    kind = models.CharField(max_length=24)
    document_date = models.DateField()
    snapshot = models.JSONField()
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        "registration",
                        "subscription",
                        "token_collection",
                        "token_refund",
                        "adjustment",
                    ]
                ),
                name="receipt_doc_kind_valid",
            )
        ]

    def __str__(self):
        return f"{self.kind} receipt {self.document_date}"


class FinancialTransaction(models.Model):
    class Kind(models.TextChoices):
        SUBSCRIPTION_PAYMENT = "subscription_payment", "Subscription payment"
        TOKEN_COLLECTION = "token_collection", "Token collection"
        TOKEN_REFUND = "token_refund", "Token refund"
        TOKEN_FORFEITURE = "token_forfeiture", "Token forfeiture"
        ADJUSTMENT = "adjustment", "Adjustment"
        REVERSAL = "reversal", "Reversal"
        # Deliberately no subscription-fee refund kind: fees are never refundable (B7).

    class CashFlow(models.TextChoices):
        IN = "in", "Money in"
        OUT = "out", "Money out"
        NONE = "none", "No cash"

    class Sequence(models.TextChoices):
        SUBSCRIPTION = "subscription", "SUB"
        TOKEN_COLLECTION = "token_collection", "TOK"
        TOKEN_REFUND = "token_refund", "REF"
        ADJUSTMENT = "adjustment", "ADJ"

    internal_txn_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    member = models.ForeignKey(
        "members.Member", on_delete=models.PROTECT, related_name="transactions"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    cash_flow = models.CharField(max_length=4, choices=CashFlow.choices)
    amount_paise = models.PositiveBigIntegerField()
    effective_date = models.DateField()
    processed_at = models.DateTimeField(default=timezone.now)
    receipt_sequence = models.CharField(max_length=20, blank=True, choices=Sequence.choices)
    receipt_issued_on = models.DateField(null=True, blank=True)
    receipt_year = models.PositiveSmallIntegerField(null=True, blank=True)
    receipt_no = models.CharField(max_length=24, blank=True)
    receipt_document = models.ForeignKey(
        ReceiptDocument, null=True, blank=True, on_delete=models.PROTECT, related_name="lines"
    )
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    adjusts = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="adjustments"
    )
    reason = models.TextField(blank=True)
    backdated = models.BooleanField(default=False)
    backdate_reason = models.TextField(blank=True)
    backdate_financial_confirmed = models.BooleanField(default=False)
    request_key = models.UUIDField(null=True, blank=True, unique=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    RECEIPT_KINDS = ("subscription_payment", "token_collection", "token_refund", "adjustment")

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="txn_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(amount_paise__lte=MAX_PAISE), name="txn_amount_within_limit"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        "subscription_payment",
                        "token_collection",
                        "token_refund",
                        "token_forfeiture",
                        "adjustment",
                        "reversal",
                    ]
                ),
                name="txn_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(cash_flow__in=["in", "out", "none"]), name="txn_cash_flow_valid"
            ),
            # The direction of money is fixed for the kinds that have one.
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(kind__in=["subscription_payment", "token_collection"])
                        & models.Q(cash_flow="in")
                    )
                    | (models.Q(kind="token_refund") & models.Q(cash_flow="out"))
                    | (models.Q(kind="token_forfeiture") & models.Q(cash_flow="none"))
                    | models.Q(kind__in=["adjustment", "reversal"])
                ),
                name="txn_cash_flow_matches_kind",
            ),
            # Receipt fields are all present for receipt kinds and all empty otherwise.
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            kind__in=[
                                "subscription_payment",
                                "token_collection",
                                "token_refund",
                                "adjustment",
                            ]
                        )
                        & ~models.Q(receipt_no="")
                        & ~models.Q(receipt_sequence="")
                        & models.Q(receipt_issued_on__isnull=False)
                        & models.Q(receipt_year__isnull=False)
                        & models.Q(receipt_document__isnull=False)
                    )
                    | (
                        models.Q(kind__in=["token_forfeiture", "reversal"])
                        & models.Q(receipt_no="")
                        & models.Q(receipt_sequence="")
                        & models.Q(receipt_issued_on__isnull=True)
                        & models.Q(receipt_year__isnull=True)
                        & models.Q(receipt_document__isnull=True)
                    )
                ),
                name="txn_receipt_fields_by_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(receipt_sequence="")
                    | (
                        models.Q(
                            receipt_sequence="subscription",
                            receipt_no__startswith="SUB-",
                            kind="subscription_payment",
                        )
                        | models.Q(
                            receipt_sequence="token_collection",
                            receipt_no__startswith="TOK-",
                            kind="token_collection",
                        )
                        | models.Q(
                            receipt_sequence="token_refund",
                            receipt_no__startswith="REF-",
                            kind="token_refund",
                        )
                        | models.Q(
                            receipt_sequence="adjustment",
                            receipt_no__startswith="ADJ-",
                            kind="adjustment",
                        )
                    )
                ),
                name="txn_receipt_prefix_matches_sequence",
            ),
            models.UniqueConstraint(
                fields=["receipt_sequence", "receipt_year", "receipt_no"],
                condition=~models.Q(receipt_no=""),
                name="txn_receipt_unique_per_sequence_year",
            ),
            models.UniqueConstraint(
                fields=["receipt_no"],
                condition=~models.Q(receipt_no=""),
                name="txn_receipt_no_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="reversal", reverses__isnull=False)
                    | (~models.Q(kind="reversal") & models.Q(reverses__isnull=True))
                ),
                name="txn_reversal_links_original",
            ),
            models.CheckConstraint(
                condition=models.Q(adjusts__isnull=True) | models.Q(kind="adjustment"),
                name="txn_only_adjustments_link_an_original",
            ),
            models.CheckConstraint(
                condition=~models.Q(kind__in=["adjustment", "reversal"], reason=""),
                name="txn_reversal_adjustment_need_reason",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(backdated=False)
                    | (~models.Q(backdate_reason="") & models.Q(backdate_financial_confirmed=True))
                ),
                name="txn_backdating_needs_reason_and_confirmation",
            ),
        ]
        indexes = [
            models.Index(fields=["member", "effective_date"], name="txn_member_date_idx"),
            models.Index(fields=["kind", "effective_date"], name="txn_kind_date_idx"),
        ]

    def __str__(self):
        return f"{self.kind} {self.receipt_no or self.internal_txn_id}"


class PaymentEntry(models.Model):
    """One cash or UPI component of a transaction (split payments settle one obligation)."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"

    transaction = models.ForeignKey(
        FinancialTransaction, on_delete=models.PROTECT, related_name="entries"
    )
    method = models.CharField(max_length=4, choices=Method.choices)
    amount_paise = models.PositiveBigIntegerField()
    paid_on = models.DateField()
    reference = models.CharField(max_length=120, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="entry_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(amount_paise__lte=MAX_PAISE), name="entry_amount_within_limit"
            ),
            models.CheckConstraint(
                condition=models.Q(method__in=["cash", "upi"]), name="entry_method_valid"
            ),
        ]
        indexes = [models.Index(fields=["paid_on", "method"], name="entry_paid_method_idx")]

    def __str__(self):
        return f"{self.method} {self.amount_paise}"


class TokenAccount(models.Model):
    """One per member; holds the member's token cycles. Never changes after creation."""

    member = models.OneToOneField(
        "members.Member", on_delete=models.PROTECT, related_name="token_account"
    )

    def __str__(self):
        return f"token account {self.member_id}"


class TokenCycle(models.Model):
    """A refundable-token cycle: the first joining, and again on every rejoining (B15)."""

    account = models.ForeignKey(TokenAccount, on_delete=models.PROTECT, related_name="cycles")
    cycle_no = models.PositiveSmallIntegerField()
    amount_snapshot_paise = models.PositiveBigIntegerField()
    started_on = models.DateField()

    class Meta:
        ordering = ["cycle_no"]
        constraints = [
            models.UniqueConstraint(fields=["account", "cycle_no"], name="token_cycle_no_unique"),
            models.CheckConstraint(
                condition=models.Q(amount_snapshot_paise__gt=0), name="token_cycle_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(amount_snapshot_paise__lte=MAX_PAISE),
                name="token_cycle_amount_within_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_no__gte=1), name="token_cycle_no_valid"
            ),
        ]

    def __str__(self):
        return f"cycle {self.cycle_no}"


class TokenLedgerEntry(models.Model):
    """The token ledger (not revenue). Collection, carry-forward, refund, forfeiture, reversal."""

    class EntryType(models.TextChoices):
        COLLECTION = "collection", "Collection"
        CARRY_FORWARD = "carry_forward", "Carried forward"
        REFUND = "refund", "Refund"
        FORFEITURE = "forfeiture", "Forfeiture"
        REVERSAL = "reversal", "Reversal"

    class Trigger(models.TextChoices):
        OVERDUE_AUTO = "overdue_auto", "Overdue (automatic)"
        GRACE_CHANGE = "grace_change_confirmed", "Grace-period change (confirmed)"
        DAMAGED = "damaged", "Damaged return"
        POOR = "poor", "Poor-condition return"
        LOST = "lost", "Lost book"

    cycle = models.ForeignKey(TokenCycle, on_delete=models.PROTECT, related_name="entries")
    entry_type = models.CharField(max_length=14, choices=EntryType.choices)
    delta_paise = models.BigIntegerField()
    forfeiture_trigger = models.CharField(max_length=24, blank=True, choices=Trigger.choices)
    owner_confirmed = models.BooleanField(default=False)
    transaction = models.ForeignKey(
        FinancialTransaction, on_delete=models.PROTECT, related_name="token_entries"
    )
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    effective_date = models.DateField()
    processed_at = models.DateTimeField(default=timezone.now)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(delta_paise__gte=-MAX_PAISE, delta_paise__lte=MAX_PAISE),
                name="token_entry_delta_within_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    entry_type__in=[
                        "collection",
                        "carry_forward",
                        "refund",
                        "forfeiture",
                        "reversal",
                    ]
                ),
                name="token_entry_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(entry_type="collection", delta_paise__gt=0)
                    | models.Q(entry_type="carry_forward", delta_paise=0)
                    | models.Q(entry_type__in=["refund", "forfeiture"], delta_paise__lt=0)
                    | models.Q(entry_type="reversal", reverses__isnull=False)
                ),
                name="token_entry_delta_matches_type",
            ),
            # one collection / refund / forfeiture per cycle: no double refund or forfeit
            models.UniqueConstraint(
                fields=["cycle"],
                condition=models.Q(entry_type="collection"),
                name="token_one_collection_per_cycle",
            ),
            models.UniqueConstraint(
                fields=["cycle"],
                condition=models.Q(entry_type="refund"),
                name="token_one_refund_per_cycle",
            ),
            models.UniqueConstraint(
                fields=["cycle"],
                condition=models.Q(entry_type="forfeiture"),
                name="token_one_forfeiture_per_cycle",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        entry_type="forfeiture",
                        forfeiture_trigger__in=[
                            "overdue_auto",
                            "grace_change_confirmed",
                            "damaged",
                            "poor",
                            "lost",
                        ],
                    )
                    | (~models.Q(entry_type="forfeiture") & models.Q(forfeiture_trigger=""))
                ),
                name="token_forfeiture_trigger_by_type",
            ),
            # only the automatic day-8 forfeiture may be unconfirmed; confirmed ones need a reason
            models.CheckConstraint(
                condition=(
                    ~models.Q(entry_type="forfeiture")
                    | models.Q(owner_confirmed=True)
                    | models.Q(forfeiture_trigger="overdue_auto")
                ),
                name="token_forfeiture_needs_confirmation",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(entry_type="forfeiture", owner_confirmed=True) & models.Q(reason="")
                ),
                name="token_confirmed_forfeiture_needs_reason",
            ),
        ]

    def __str__(self):
        return f"{self.entry_type} {self.delta_paise}"
