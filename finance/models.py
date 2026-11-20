from django.db import models
from django.db.models import Q

from core.fields import money
from core.models import ImmutableModel


class Receipt(ImmutableModel):
    MODES = [("cash", "Cash"), ("upi", "UPI"), ("bank", "Bank transfer"), ("cheque", "Cheque")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30)
    customer = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="receipts")
    account = models.ForeignKey("core.FinancialAccount", on_delete=models.PROTECT, related_name="+")
    amount = money()
    receipt_date = models.DateField()
    mode = models.CharField(max_length=10, choices=MODES, default="cash")
    reference = models.CharField(max_length=60, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["business", "number"], name="uniq_receipt_number"),
                       models.CheckConstraint(condition=Q(amount__gt=0), name="receipt_positive")]


class ReceiptAllocation(ImmutableModel):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey("sales.SalesInvoice", on_delete=models.PROTECT, related_name="receipt_allocations")
    amount = money()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="ra_positive")]


class SupplierPayment(ImmutableModel):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30)
    supplier = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="payments")
    account = models.ForeignKey("core.FinancialAccount", on_delete=models.PROTECT, related_name="+")
    amount = money()
    payment_date = models.DateField()
    mode = models.CharField(max_length=10, choices=Receipt.MODES, default="bank")
    reference = models.CharField(max_length=60, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["business", "number"], name="uniq_spay_number"),
                       models.CheckConstraint(condition=Q(amount__gt=0), name="spay_positive")]


class SupplierPaymentAllocation(ImmutableModel):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    payment = models.ForeignKey(SupplierPayment, on_delete=models.PROTECT, related_name="allocations")
    bill = models.ForeignKey("purchasing.PurchaseBill", on_delete=models.PROTECT, related_name="payment_allocations")
    amount = money()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="spa_positive")]


class Expense(ImmutableModel):
    CATEGORIES = [("rent", "Rent"), ("electricity", "Electricity"), ("transport", "Transport"),
                  ("maintenance", "Machine maintenance"), ("salary_other", "Staff salary"),
                  ("tea_snacks", "Tea & snacks"), ("misc", "Miscellaneous")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30)
    category = models.CharField(max_length=20, choices=CATEGORIES)
    description = models.CharField(max_length=200, blank=True)
    account = models.ForeignKey("core.FinancialAccount", on_delete=models.PROTECT, related_name="+")
    amount = money()
    expense_date = models.DateField()
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["business", "number"], name="uniq_expense_number"),
                       models.CheckConstraint(condition=Q(amount__gt=0), name="expense_positive")]


class CashBookEntry(ImmutableModel):
    IN, OUT = "in", "out"
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    account = models.ForeignKey("core.FinancialAccount", on_delete=models.PROTECT, related_name="cashbook_entries")
    entry_date = models.DateField()
    direction = models.CharField(max_length=3, choices=[(IN, "In"), (OUT, "Out")])
    amount = money()
    narration = models.CharField(max_length=200, blank=True)
    # explicit FKs, exactly one source
    receipt = models.OneToOneField(Receipt, null=True, blank=True, on_delete=models.PROTECT, related_name="cashbook_entry")
    supplier_payment = models.OneToOneField(SupplierPayment, null=True, blank=True, on_delete=models.PROTECT, related_name="cashbook_entry")
    expense = models.OneToOneField(Expense, null=True, blank=True, on_delete=models.PROTECT, related_name="cashbook_entry")
    wage_payment = models.OneToOneField("payroll.WagePayment", null=True, blank=True, on_delete=models.PROTECT, related_name="cashbook_entry")
    worker_advance = models.OneToOneField("payroll.WorkerAdvance", null=True, blank=True, on_delete=models.PROTECT, related_name="cashbook_entry")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-entry_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="cashbook_positive"),
            models.CheckConstraint(
                condition=(Q(receipt__isnull=False, supplier_payment__isnull=True, expense__isnull=True, wage_payment__isnull=True, worker_advance__isnull=True)
                           | Q(receipt__isnull=True, supplier_payment__isnull=False, expense__isnull=True, wage_payment__isnull=True, worker_advance__isnull=True)
                           | Q(receipt__isnull=True, supplier_payment__isnull=True, expense__isnull=False, wage_payment__isnull=True, worker_advance__isnull=True)
                           | Q(receipt__isnull=True, supplier_payment__isnull=True, expense__isnull=True, wage_payment__isnull=False, worker_advance__isnull=True)
                           | Q(receipt__isnull=True, supplier_payment__isnull=True, expense__isnull=True, wage_payment__isnull=True, worker_advance__isnull=False)),
                name="cashbook_exactly_one_source"),
        ]
