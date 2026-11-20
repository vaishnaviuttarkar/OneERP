from decimal import Decimal

from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from .models import PurchaseBill


def bills_with_outstanding(business, supplier=None, only_open=False):
    qs = PurchaseBill.objects.filter(business=business, status="posted").annotate(
        paid=Coalesce(Sum("payment_allocations__amount"), Value(Decimal("0"), output_field=DecimalField(max_digits=14, decimal_places=2))))
    if supplier:
        qs = qs.filter(supplier=supplier)
    bills = list(qs.select_related("supplier").order_by("bill_date", "id"))
    for b in bills:
        b.outstanding = b.total - b.paid
    return [b for b in bills if b.outstanding > 0] if only_open else bills


def total_payable(business):
    return sum((b.outstanding for b in bills_with_outstanding(business)), 0)
