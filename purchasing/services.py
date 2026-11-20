from django.db import IntegrityError, transaction
from django.utils import timezone

from catalogue.services import require_variant
from core.exceptions import DomainError
from core.services import next_number, record_audit
from core.utils import D, ZERO, r2
from inventory.models import StockMovement
from inventory.services import post_movement
from parties.services import require_party
from .models import PurchaseBill, PurchaseBillLine


@transaction.atomic
def create_bill(*, business, user, supplier_id, supplier_bill_number, bill_date, lines):
    """lines: [{variant_id, quantity, unit_cost, tax_amount}] -> draft bill"""
    supplier = require_party(business, supplier_id, supplier=True)
    sbn = (supplier_bill_number or "").strip()
    if not sbn:
        raise DomainError("Supplier bill number is required.")
    if not lines:
        raise DomainError("A purchase bill needs at least one line.")
    if bill_date > timezone.localdate():
        raise DomainError("Bill date cannot be in the future.")
    try:
        with transaction.atomic():
            bill = PurchaseBill.objects.create(business=business, supplier=supplier, supplier_bill_number=sbn,
                                               bill_date=bill_date, created_by=user)
    except IntegrityError:
        raise DomainError(f"Supplier bill number {sbn} already exists for {supplier.name}.")
    sub = tax = total = ZERO
    for ln in lines:
        v = require_variant(business, ln["variant_id"])
        if v.product.kind == "finished":
            raise DomainError(f"{v.sku}: finished goods are produced, not purchased.")
        q, cost, t = D(ln["quantity"]), D(ln["unit_cost"]), D(ln.get("tax_amount") or 0)
        if q <= 0 or cost < 0 or t < 0:
            raise DomainError("Quantity must be positive; cost and tax cannot be negative.")
        amount = r2(q * cost)
        if t > amount:
            raise DomainError(f"{v.sku}: tax cannot exceed the line amount.")
        PurchaseBillLine.objects.create(business=business, bill=bill, variant=v, quantity=q,
                                        unit_cost=cost, line_amount=amount, tax_amount=t)
        total += amount
        tax += t
    bill.subtotal, bill.tax_total, bill.total = total - tax, tax, total
    PurchaseBill.objects.filter(pk=bill.pk).update(subtotal=total - tax, tax_total=tax, total=total)
    record_audit(business=business, user=user, action="purchase_bill.create", entity="PurchaseBill",
                 entity_id=bill.pk, after={"total": total})
    return bill


@transaction.atomic
def delete_draft_bill(*, business, user, bill_id):
    bill = PurchaseBill.objects.select_for_update().get(pk=bill_id, business=business)
    if bill.status != PurchaseBill.DRAFT:
        raise DomainError("Posted purchase bills cannot be deleted.")
    record_audit(business=business, user=user, action="purchase_bill.delete_draft", entity="PurchaseBill",
                 entity_id=bill.pk, before={"supplier_bill_number": bill.supplier_bill_number})
    bill.lines.all().delete()
    PurchaseBill.objects.filter(pk=bill.pk).delete()


@transaction.atomic
def post_bill(*, business, user, bill_id):
    bill = PurchaseBill.objects.select_for_update().get(pk=bill_id, business=business)
    if bill.status != PurchaseBill.DRAFT:
        raise DomainError("Bill is already posted.")
    require_party(business, bill.supplier_id, supplier=True)
    number = next_number(business, "purchase_bill")
    for line in bill.lines.select_related("variant__product").order_by("id"):
        mv = post_movement(
            business=business, variant=line.variant, movement_type=StockMovement.PURCHASE,
            quantity=line.quantity, value=line.line_amount - line.tax_amount,  # recoverable tax excluded
            user=user, source_type="purchase_bill", source_id=bill.pk, note=number)
        PurchaseBillLine.objects.filter(pk=line.pk).update(stock_movement=mv)
    PurchaseBill.objects.filter(pk=bill.pk).update(status=PurchaseBill.POSTED, number=number,
                                                   posted_at=timezone.now())
    record_audit(business=business, user=user, action="purchase_bill.post", entity="PurchaseBill",
                 entity_id=bill.pk, after={"number": number, "total": bill.total})
    bill.refresh_from_db()
    return bill
