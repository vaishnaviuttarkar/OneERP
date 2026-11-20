from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q, Value

from core.fields import money
from core.models import ImmutableModel, PostedGuardModel


class WageStatement(PostedGuardModel):
    DRAFT, FINALIZED = "draft", "finalized"
    STATUSES = [(DRAFT, "Draft"), (FINALIZED, "Finalized")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30, blank=True)
    worker = models.ForeignKey("production.Worker", on_delete=models.PROTECT, related_name="wage_statements")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    gross_earnings = money(default=0)
    additional_earnings = money(default=0)
    deductions = money(default=0)
    advance_recovery = money(default=0)
    net_payable = money(default=0)
    paid_amount = money(default=0)
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(period_end__gte=F("period_start")), name="ws_period_valid"),
            models.CheckConstraint(condition=Q(paid_amount__gte=0) & Q(paid_amount__lte=F("net_payable")) | Q(status="draft"),
                                   name="ws_paid_within_net"),
            models.UniqueConstraint(fields=["business", "number"], condition=~Q(number=""), name="uniq_ws_number"),
            ExclusionConstraint(
                name="ws_no_overlap_per_worker",
                expressions=[
                    ("business", RangeOperators.EQUAL),
                    ("worker", RangeOperators.EQUAL),
                    (Func(F("period_start"), F("period_end"), Value("[]"), function="daterange",
                          output_field=DateRangeField()), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    @property
    def outstanding(self):
        return self.net_payable - self.paid_amount


class WageStatementLine(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    statement = models.ForeignKey(WageStatement, on_delete=models.CASCADE, related_name="lines")
    # OneToOne: a work entry belongs to at most one wage statement
    work_entry = models.OneToOneField("production.WorkEntry", on_delete=models.PROTECT, related_name="wage_line")
    amount = money()


class WageAdjustment(models.Model):
    EARNING, DEDUCTION = "earning", "deduction"
    KINDS = [(EARNING, "Additional earning"), (DEDUCTION, "Deduction")]
    PROPOSED, APPROVED, REJECTED = "proposed", "approved", "rejected"
    STATUSES = [(PROPOSED, "Proposed"), (APPROVED, "Approved"), (REJECTED, "Rejected")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    statement = models.ForeignKey(WageStatement, on_delete=models.CASCADE, related_name="adjustments")
    kind = models.CharField(max_length=10, choices=KINDS)
    amount = money()
    reason = models.CharField(max_length=300)
    rejection_outcome = models.ForeignKey("production.StageOutcome", null=True, blank=True,
                                          on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=STATUSES, default=PROPOSED)
    proposed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0) & ~Q(reason=""), name="wa_amount_reason"),
            models.CheckConstraint(condition=Q(kind="earning") | Q(rejection_outcome__isnull=False),
                                   name="wa_deduction_needs_rejection_ref"),
        ]


class WageAdjustmentDecision(ImmutableModel):
    adjustment = models.OneToOneField(WageAdjustment, on_delete=models.PROTECT, related_name="decision")
    approved = models.BooleanField()
    decided_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    is_self_approval = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)


class WorkerAdvance(ImmutableModel):
    ISSUED, RECOVERED = "issued", "recovered"
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    worker = models.ForeignKey("production.Worker", on_delete=models.PROTECT, related_name="advances")
    direction = models.CharField(max_length=10, choices=[(ISSUED, "Issued"), (RECOVERED, "Recovered")])
    source = models.CharField(max_length=20, default="cash_advance")   # cash_advance | excess_payment | statement_recovery
    amount = money()
    business_date = models.DateField()
    statement = models.ForeignKey(WageStatement, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="advance_positive")]


class WagePayment(ImmutableModel):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    statement = models.ForeignKey(WageStatement, on_delete=models.PROTECT, related_name="payments")
    account = models.ForeignKey("core.FinancialAccount", on_delete=models.PROTECT, related_name="+")
    amount = money()            # cash paid out
    applied_amount = money()    # reduces outstanding wages
    excess_amount = money(default=0)   # recorded as worker advance
    payment_date = models.DateField()
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=Q(amount__gt=0, amount=F("applied_amount") + F("excess_amount")), name="wp_amount_split")]
