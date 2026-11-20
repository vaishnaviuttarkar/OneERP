from django.db import models
from django.db.models import F, Q

from core.fields import money, qty
from core.models import ImmutableModel


class StockBalance(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT, related_name="stock_balances")
    quantity = qty(default=0)
    value = money(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "variant"], name="uniq_stock_balance"),
            models.CheckConstraint(condition=Q(quantity__gte=0), name="stock_qty_non_negative"),
            models.CheckConstraint(condition=Q(value__gte=0), name="stock_value_non_negative"),
        ]


class StockMovement(ImmutableModel):
    OPENING, PURCHASE, ISSUE, FG_RECEIPT = "opening", "purchase_receipt", "material_issue", "fg_receipt"
    ADJUSTMENT, DAMAGE, DISPATCH, DISPATCH_REV = "adjustment", "damage", "dispatch", "dispatch_reversal"
    TYPES = [(OPENING, "Opening stock"), (PURCHASE, "Purchase receipt"), (ISSUE, "Material issue"),
             (FG_RECEIPT, "Finished goods receipt"), (ADJUSTMENT, "Adjustment"), (DAMAGE, "Damage / rejection"),
             (DISPATCH, "Dispatch"), (DISPATCH_REV, "Dispatch reversal")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=TYPES)
    quantity = qty()          # signed: + in, - out
    value = money()           # signed
    unit_cost = models.DecimalField(max_digits=16, decimal_places=6, default=0)
    business_date = models.DateField()
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.PositiveBigIntegerField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(quantity=0), name="movement_qty_nonzero"),
            models.CheckConstraint(
                condition=(Q(quantity__gt=0, value__gte=0) | Q(quantity__lt=0, value__lte=0)),
                name="movement_value_sign_matches_qty"),
        ]
        indexes = [models.Index(fields=["business", "variant", "id"])]
