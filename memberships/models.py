from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Value

from core.money import MAX_PAISE


class SubscriptionPlan(models.Model):
    """A purchasable plan. Subscriptions snapshot name, duration and price, so editing a plan
    never changes history."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    duration_months = models.PositiveSmallIntegerField()
    price_paise = models.PositiveBigIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["duration_months"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(duration_months__gt=0), name="plan_duration_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(price_paise__gt=0), name="plan_price_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(price_paise__lte=MAX_PAISE), name="plan_price_within_limit"
            ),
        ]

    def __str__(self):
        return self.name


class MemberSubscription(models.Model):
    """One subscription period. Never deleted; the only permitted change is a one-time
    cancellation (no financial effect: fees are never refunded). Dates are fixed at creation."""

    member = models.ForeignKey(
        "members.Member", on_delete=models.PROTECT, related_name="subscriptions"
    )
    token_cycle = models.ForeignKey(
        "finance.TokenCycle", on_delete=models.PROTECT, related_name="subscriptions"
    )
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT, related_name="+")
    plan_name_snapshot = models.CharField(max_length=80)
    duration_months_snapshot = models.PositiveSmallIntegerField()
    price_paise_snapshot = models.PositiveBigIntegerField()
    starts_on = models.DateField(help_text="Inclusive.")
    expires_on = models.DateField(
        help_text="The FIRST EXPIRED day; last valid day = expires_on - 1."
    )
    payment_txn = models.OneToOneField(
        "finance.FinancialTransaction", on_delete=models.PROTECT, related_name="subscription"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_on"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_on__gt=models.F("starts_on")),
                name="subscription_expiry_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(price_paise_snapshot__gt=0, duration_months_snapshot__gt=0),
                name="subscription_snapshots_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(price_paise_snapshot__lte=MAX_PAISE),
                name="subscription_price_within_limit",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(cancelled_at__isnull=True, cancel_reason="")
                    | (models.Q(cancelled_at__isnull=False) & ~models.Q(cancel_reason=""))
                ),
                name="subscription_cancel_needs_reason",
            ),
            # Periods of one member never overlap (cancelled ones are ignored).
            ExclusionConstraint(
                name="subscription_no_overlap",
                expressions=[
                    ("member", RangeOperators.EQUAL),
                    (
                        Func(
                            F("starts_on"),
                            F("expires_on"),
                            Value("[)"),
                            function="daterange",
                            output_field=DateRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=models.Q(cancelled_at__isnull=True),
            ),
        ]
        indexes = [
            models.Index(fields=["member", "expires_on"], name="subscription_member_exp_idx")
        ]

    def __str__(self):
        return f"{self.member_id} {self.starts_on}..{self.expires_on}"

    @property
    def last_valid_day(self):
        from . import policy

        return policy.last_valid_day(self.expires_on)

    @property
    def period(self):
        from .policy import Period

        return Period(self.starts_on, self.expires_on, self.cancelled_at is not None)
