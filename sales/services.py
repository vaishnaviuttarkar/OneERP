from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from catalogue.services import require_variant
from core.exceptions import DomainError
from core.services import next_number, record_audit
from core.utils import D, ZERO, r2
from inventory.models import StockMovement
from inventory.services import post_movement
from parties.services import require_party
from .models import DeliveryChallan, DeliveryChallanLine, SalesInvoice, SalesInvoiceLine

ACTIVE_INVOICE_STATUSES = ("draft", "posted")


def _int_qty(v, label="Quantity"):
    q = D(v)
    if q <= 0 or q != q.to_integral_value():
        raise DomainError(f"{label} must be a whole number of pieces greater than zero.")
    return int(q)


def active_invoiced_qty(challan_line, exclude_invoice=None):
    qs = SalesInvoiceLine.objects.filter(challan_line=challan_line, invoice__status__in=ACTIVE_INVOICE_STATUSES)
    if exclude_invoice is not None:
        qs = qs.exclude(invoice=exclude_invoice)
    return qs.aggregate(t=Sum("quantity"))["t"] or 0


# ------------------------------------------------------------------ challans
@transaction.atomic
def create_challan(*, business, user, customer_id, challan_date, lines, vehicle_no="", notes=""):
    cust = require_party(business, customer_id, customer=True)
    if not lines:
        raise DomainError("A challan needs at least one line.")
    ch = DeliveryChallan.objects.create(business=business, customer=cust, challan_date=challan_date,
                                        vehicle_no=vehicle_no, notes=notes, created_by=user)
    for ln in lines:
        v = require_variant(business, ln["variant_id"], kind="finished")
        DeliveryChallanLine.objects.create(business=business, challan=ch, variant=v,
                                           quantity=_int_qty(ln["quantity"]))
    record_audit(business=business, user=user, action="challan.create", entity="DeliveryChallan", entity_id=ch.pk)
    return ch


@transaction.atomic
def post_challan(*, business, user, challan_id):
    ch = DeliveryChallan.objects.select_for_update().get(pk=challan_id, business=business)
    if ch.status != DeliveryChallan.DRAFT:
        raise DomainError("Challan is not a draft.")
    require_party(business, ch.customer_id, customer=True)
    number = next_number(business, "delivery_challan")
    for line in ch.lines.select_related("variant").order_by("id"):
        mv = post_movement(business=business, variant=line.variant, movement_type=StockMovement.DISPATCH,
                           quantity=-D(line.quantity), user=user, source_type="delivery_challan",
                           source_id=ch.pk, note=number)    # stock leaves exactly once here
        DeliveryChallanLine.objects.filter(pk=line.pk).update(movement=mv)
    DeliveryChallan.objects.filter(pk=ch.pk).update(status=DeliveryChallan.POSTED, number=number, posted_at=timezone.now())
    record_audit(business=business, user=user, action="challan.post", entity="DeliveryChallan", entity_id=ch.pk,
                 after={"number": number})
    ch.refresh_from_db()
    return ch


@transaction.atomic
def cancel_challan(*, business, user, challan_id):
    ch = DeliveryChallan.objects.select_for_update().get(pk=challan_id, business=business)
    if ch.status != DeliveryChallan.POSTED:
        raise DomainError("Only posted challans can be cancelled.")
    lines = list(ch.lines.select_for_update().select_related("variant").order_by("id"))
    for line in lines:
        if active_invoiced_qty(line) > 0:
            raise DomainError(f"{line.variant.sku} has active invoiced quantity; cancel those invoices first.")
    for line in lines:
        mv = post_movement(business=business, variant=line.variant, movement_type=StockMovement.DISPATCH_REV,
                           quantity=D(line.quantity), user=user, source_type="delivery_challan",
                           source_id=ch.pk, note=f"Cancel {ch.number}")
        DeliveryChallanLine.objects.filter(pk=line.pk).update(reversal_movement=mv)
    DeliveryChallan.objects.filter(pk=ch.pk).update(status=DeliveryChallan.CANCELLED)
    record_audit(business=business, user=user, action="challan.cancel", entity="DeliveryChallan", entity_id=ch.pk)


@transaction.atomic
def delete_draft_challan(*, business, user, challan_id):
    ch = DeliveryChallan.objects.select_for_update().get(pk=challan_id, business=business)
    if ch.status != DeliveryChallan.DRAFT:
        raise DomainError("Only draft challans can be deleted.")
    ch.lines.all().delete()
    DeliveryChallan.objects.filter(pk=ch.pk).delete()


# ------------------------------------------------------------------ invoices
def _validate_lines(business, customer, lines, invoice=None):
    """Lock referenced challan lines (ordered) and validate cumulative invoiced <= dispatched."""
    prepared, need = [], {}
    for ln in lines:
        v = require_variant(business, ln["variant_id"], kind="finished")
        q = _int_qty(ln["quantity"])
        rate = D(ln.get("rate"))
        if rate < 0:
            raise DomainError("Rate cannot be negative.")
        cl_id = ln.get("challan_line_id")
        prepared.append((v, q, rate, cl_id))
        if cl_id:
            need[cl_id] = need.get(cl_id, 0) + q
    locked = {cl.pk: cl for cl in DeliveryChallanLine.objects.select_for_update(of=("self",))
              .select_related("challan").filter(pk__in=need.keys(), business=business).order_by("id")}
    for cl_id, q in need.items():
        cl = locked.get(cl_id)
        if cl is None:
            raise DomainError("Challan line not found.")
        if cl.challan.status != DeliveryChallan.POSTED:
            raise DomainError(f"Challan {cl.challan.number or cl.challan_id} is not posted.")
        if cl.challan.customer_id != customer.pk:
            raise DomainError("Challan belongs to a different customer.")
        remaining = cl.quantity - active_invoiced_qty(cl, exclude_invoice=invoice)
        if q > remaining:
            raise DomainError(f"{cl.variant.sku}: only {remaining} pieces remain to invoice on challan {cl.challan.number}.")
    for v, q, rate, cl_id in prepared:
        if cl_id and locked[cl_id].variant_id != v.pk:
            raise DomainError("Invoice line variant does not match the challan line.")
    return prepared


def _totals(prepared):
    sub = sum((r2(D(q) * rate) for _, q, rate, _ in prepared), ZERO)
    total = sub.quantize(Decimal("1"), rounding=ROUND_HALF_UP)     # nearest whole rupee
    return sub, total - sub, total


@transaction.atomic
def create_invoice(*, business, user, customer_id, invoice_date, lines, notes=""):
    cust = require_party(business, customer_id, customer=True)
    if not lines:
        raise DomainError("An invoice needs at least one line.")
    prepared = _validate_lines(business, cust, lines)
    sub, ro, total = _totals(prepared)
    inv = SalesInvoice.objects.create(business=business, customer=cust, invoice_date=invoice_date, notes=notes,
                                      subtotal=sub, round_off=ro, total=total, created_by=user)
    for v, q, rate, cl_id in prepared:
        SalesInvoiceLine.objects.create(business=business, invoice=inv, variant=v, quantity=q, rate=rate,
                                        amount=r2(D(q) * rate), challan_line_id=cl_id)
    record_audit(business=business, user=user, action="invoice.create", entity="SalesInvoice", entity_id=inv.pk)
    return inv


@transaction.atomic
def post_invoice(*, business, user, invoice_id):
    inv = SalesInvoice.objects.select_for_update().get(pk=invoice_id, business=business)
    if inv.status != SalesInvoice.DRAFT:
        raise DomainError("Invoice is not a draft.")
    cust = require_party(business, inv.customer_id, customer=True)
    lines = list(inv.lines.select_related("variant").order_by("id"))
    prepared = _validate_lines(business, cust, [
        {"variant_id": l.variant_id, "quantity": l.quantity, "rate": l.rate, "challan_line_id": l.challan_line_id}
        for l in lines], invoice=inv)
    sub, ro, total = _totals(prepared)
    number = next_number(business, "sales_invoice")
    for line in lines:
        if line.challan_line_id is None:     # direct line: dispatch now. Challan lines never dispatch again.
            mv = post_movement(business=business, variant=line.variant, movement_type=StockMovement.DISPATCH,
                               quantity=-D(line.quantity), user=user, source_type="sales_invoice",
                               source_id=inv.pk, note=number)
            SalesInvoiceLine.objects.filter(pk=line.pk).update(movement=mv)
    SalesInvoice.objects.filter(pk=inv.pk).update(status=SalesInvoice.POSTED, number=number, subtotal=sub,
                                                  round_off=ro, total=total, posted_at=timezone.now())
    record_audit(business=business, user=user, action="invoice.post", entity="SalesInvoice", entity_id=inv.pk,
                 after={"number": number, "total": total})
    inv.refresh_from_db()
    return inv


@transaction.atomic
def cancel_invoice(*, business, user, invoice_id):
    inv = SalesInvoice.objects.select_for_update().get(pk=invoice_id, business=business)
    if inv.status != SalesInvoice.POSTED:
        raise DomainError("Only posted invoices can be cancelled.")
    if inv.receipt_allocations.exists():
        raise DomainError("Receipts are allocated to this invoice; it cannot be cancelled.")
    for line in inv.lines.select_for_update().select_related("variant").order_by("id"):
        if line.challan_line_id is None and line.movement_id:
            mv = post_movement(business=business, variant=line.variant, movement_type=StockMovement.DISPATCH_REV,
                               quantity=D(line.quantity), user=user, source_type="sales_invoice",
                               source_id=inv.pk, note=f"Cancel {inv.number}")
            SalesInvoiceLine.objects.filter(pk=line.pk).update(reversal_movement=mv)
    SalesInvoice.objects.filter(pk=inv.pk).update(status=SalesInvoice.CANCELLED)   # releases invoiced qty
    record_audit(business=business, user=user, action="invoice.cancel", entity="SalesInvoice", entity_id=inv.pk,
                 before={"number": inv.number, "total": inv.total})


@transaction.atomic
def update_draft_invoice(*, business, user, invoice_id, customer_id, invoice_date, lines, notes=""):
    """Rewrite a draft invoice (header and lines). Posted invoices are immutable."""
    inv = SalesInvoice.objects.select_for_update().get(pk=invoice_id, business=business)
    if inv.status != SalesInvoice.DRAFT:
        raise DomainError("Only draft invoices can be edited.")
    cust = require_party(business, customer_id, customer=True)
    if not lines:
        raise DomainError("An invoice needs at least one line.")
    prepared = _validate_lines(business, cust, lines, invoice=inv)
    sub, ro, total = _totals(prepared)
    inv.lines.all().delete()
    for v, q, rate, cl_id in prepared:
        SalesInvoiceLine.objects.create(business=business, invoice=inv, variant=v, quantity=q, rate=rate,
                                        amount=r2(D(q) * rate), challan_line_id=cl_id)
    SalesInvoice.objects.filter(pk=inv.pk).update(customer=cust, invoice_date=invoice_date, notes=notes,
                                                  subtotal=sub, round_off=ro, total=total)
    record_audit(business=business, user=user, action="invoice.update", entity="SalesInvoice", entity_id=inv.pk)
    inv.refresh_from_db()
    return inv


@transaction.atomic
def update_draft_challan(*, business, user, challan_id, customer_id, challan_date, lines, vehicle_no="", notes=""):
    """Rewrite a draft challan (header and lines). Dispatch happens only when posted."""
    ch = DeliveryChallan.objects.select_for_update().get(pk=challan_id, business=business)
    if ch.status != DeliveryChallan.DRAFT:
        raise DomainError("Only draft challans can be edited.")
    cust = require_party(business, customer_id, customer=True)
    if not lines:
        raise DomainError("A challan needs at least one line.")
    ch.lines.all().delete()
    for ln in lines:
        v = require_variant(business, ln["variant_id"], kind="finished")
        DeliveryChallanLine.objects.create(business=business, challan=ch, variant=v, quantity=_int_qty(ln["quantity"]))
    DeliveryChallan.objects.filter(pk=ch.pk).update(customer=cust, challan_date=challan_date, vehicle_no=vehicle_no, notes=notes)
    record_audit(business=business, user=user, action="challan.update", entity="DeliveryChallan", entity_id=ch.pk)
    ch.refresh_from_db()
    return ch


@transaction.atomic
def delete_draft_invoice(*, business, user, invoice_id):
    inv = SalesInvoice.objects.select_for_update().get(pk=invoice_id, business=business)
    if inv.status != SalesInvoice.DRAFT:
        raise DomainError("Only draft invoices can be deleted.")
    inv.lines.all().delete()
    SalesInvoice.objects.filter(pk=inv.pk).delete()


def open_challan_lines(business, customer):
    """Posted challan lines with quantity still available to invoice."""
    out = []
    qs = DeliveryChallanLine.objects.filter(business=business, challan__customer=customer,
                                            challan__status="posted").select_related("challan", "variant__product")
    for cl in qs.order_by("challan_id", "id"):
        remaining = cl.quantity - active_invoiced_qty(cl)
        if remaining > 0:
            out.append((cl, remaining))
    return out
