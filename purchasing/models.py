from django.db import models
from django.db.models import F, Q

from core.fields import money, qty
from core.models import PostedGuardModel


class PurchaseBill(PostedGuardModel):
    DRAFT, POSTED = "draft", "posted"
    STATUSES = [(DRAFT, "Draft"), (POSTED, "Posted")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30, blank=True)
    supplier = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="purchase_bills")
    supplier_bill_number = models.CharField(max_length=50)
    bill_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    subtotal = money(default=0)
    tax_total = money(default=0)
    total = money(default=0)   # sum of line amounts (tax-inclusive)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["business", "supplier", "supplier_bill_number"],
                                    name="uniq_supplier_bill_number"),
            models.UniqueConstraint(fields=["business", "number"], condition=~Q(number=""),
                                    name="uniq_purchase_number"),
        ]


class PurchaseBillLine(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    bill = models.ForeignKey(PurchaseBill, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    quantity = qty()
    unit_cost = money()       # tax-inclusive purchase rate
    line_amount = money()     # quantity x unit_cost (includes recoverable tax)
    tax_amount = money(default=0)
    stock_movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True,
                                          on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="pbl_qty_positive"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="pbl_cost_non_negative"),
            models.CheckConstraint(condition=Q(tax_amount__gte=0) & Q(tax_amount__lte=F("line_amount")),
                                   name="pbl_tax_within_amount"),
        ]
