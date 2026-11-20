from django.db import models
from django.db.models import Q

from core.fields import money
from core.models import PostedGuardModel


class DeliveryChallan(PostedGuardModel):
    DRAFT, POSTED, CANCELLED = "draft", "posted", "cancelled"
    STATUSES = [(DRAFT, "Draft"), (POSTED, "Posted"), (CANCELLED, "Cancelled")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30, blank=True)
    customer = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="challans")
    challan_date = models.DateField()
    vehicle_no = models.CharField(max_length=20, blank=True)
    notes = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["business", "number"], condition=~Q(number=""),
                                               name="uniq_challan_number")]


class DeliveryChallanLine(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    challan = models.ForeignKey(DeliveryChallan, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()   # integer pieces
    movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reversal_movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="dcl_qty_positive")]


class SalesInvoice(PostedGuardModel):
    DRAFT, POSTED, CANCELLED = "draft", "posted", "cancelled"
    STATUSES = [(DRAFT, "Draft"), (POSTED, "Posted"), (CANCELLED, "Cancelled")]
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    number = models.CharField(max_length=30, blank=True)
    customer = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="invoices")
    invoice_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    subtotal = money(default=0)
    round_off = money(default=0)
    total = money(default=0)     # whole rupees once posted
    notes = models.CharField(max_length=200, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["business", "number"], condition=~Q(number=""),
                                               name="uniq_invoice_number")]


class SalesInvoiceLine(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name="lines")
    variant = models.ForeignKey("catalogue.ProductVariant", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    rate = money()
    amount = money()
    challan_line = models.ForeignKey(DeliveryChallanLine, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="invoice_lines")
    movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reversal_movement = models.OneToOneField("inventory.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0) & Q(rate__gte=0), name="sil_qty_rate_valid"),
            # a line either dispatches itself (direct) or consumes a challan dispatch, never both
            models.CheckConstraint(condition=Q(challan_line__isnull=True) | Q(movement__isnull=True),
                                   name="sil_no_double_dispatch"),
        ]
