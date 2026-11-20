from decimal import Decimal

from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from .models import SalesInvoice


def invoices_with_outstanding(business, customer=None, only_open=False):
    qs = SalesInvoice.objects.filter(business=business, status="posted").annotate(
        received=Coalesce(Sum("receipt_allocations__amount"), Value(Decimal("0"), output_field=DecimalField(max_digits=14, decimal_places=2))))
    if customer:
        qs = qs.filter(customer=customer)
    rows = list(qs.select_related("customer").order_by("invoice_date", "id"))
    for r in rows:
        r.outstanding = r.total - r.received
    return [r for r in rows if r.outstanding > 0] if only_open else rows


def total_receivable(business):
    return sum((r.outstanding for r in invoices_with_outstanding(business)), 0)
