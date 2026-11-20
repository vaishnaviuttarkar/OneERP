from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from catalogue.models import ProductTemplate, ProductVariant
from core.exceptions import DomainError, InsufficientStock
from core.utils import D, ZERO, r2
from .models import StockBalance, StockMovement

INFLOW_TYPES = {StockMovement.OPENING, StockMovement.PURCHASE, StockMovement.FG_RECEIPT,
                StockMovement.DISPATCH_REV}
OUTFLOW_TYPES = {StockMovement.ISSUE, StockMovement.DAMAGE, StockMovement.DISPATCH}


def _locked_balance(business, variant):
    qs = StockBalance.objects.select_for_update().filter(business=business, variant=variant)
    bal = qs.first()
    if bal is None:
        try:
            with transaction.atomic():
                StockBalance.objects.create(business=business, variant=variant)
        except IntegrityError:
            pass
        bal = qs.get()
    return bal


@transaction.atomic
def post_movement(*, business, variant, movement_type, quantity, user, value=None,
                  business_date=None, source_type="", source_id=None, note=""):
    """The ONLY way stock changes. `quantity` is signed. Weighted-average costing for materials;
    finished goods are tracked by quantity only (value 0).
    Inflows: `value` is the stock value being added (defaults to current average cost x qty).
    Outflows: value is computed (final issue consumes exact remaining value)."""
    quantity = D(quantity)
    if quantity == 0:
        raise DomainError("Quantity must be non-zero.")
    today = timezone.localdate()
    business_date = business_date or today
    if business_date < today:
        raise DomainError("Backdated stock documents are not allowed.")
    if movement_type in INFLOW_TYPES and quantity < 0:
        raise DomainError("This movement type must be an inflow.")
    if movement_type in OUTFLOW_TYPES and quantity > 0:
        raise DomainError("This movement type must be an outflow.")

    variant = ProductVariant.objects.select_related("product").get(pk=variant.pk, business=business)
    tracked = variant.product.kind != ProductTemplate.FINISHED
    bal = _locked_balance(business, variant)
    new_qty = bal.quantity + quantity
    if new_qty < 0:
        raise InsufficientStock(
            f"Insufficient stock for {variant.sku}: available {bal.quantity}, requested {abs(quantity)}.")

    if not tracked:
        signed_value = ZERO
    elif quantity > 0:
        if value is None:
            avg = (bal.value / bal.quantity) if bal.quantity else ZERO
            value = r2(avg * quantity)
        signed_value = r2(value)
        if signed_value < 0:
            raise DomainError("Stock value cannot be negative.")
    else:
        if new_qty == 0:
            out = bal.value                                    # final issue: exact remaining value
        else:
            out = r2(bal.value * abs(quantity) / bal.quantity)  # full precision, then half-up
        signed_value = -out

    unit_cost = (abs(signed_value) / abs(quantity)).quantize(Decimal("0.000001")) if tracked else ZERO
    mv = StockMovement.objects.create(
        business=business, variant=variant, movement_type=movement_type, quantity=quantity,
        value=signed_value, unit_cost=unit_cost, business_date=business_date,
        source_type=source_type, source_id=source_id, note=note, created_by=user)
    bal.quantity = new_qty
    bal.value = bal.value + signed_value
    bal.save(update_fields=["quantity", "value"])
    return mv


def available_quantity(business, variant):
    bal = StockBalance.objects.filter(business=business, variant=variant).first()
    return bal.quantity if bal else ZERO


@transaction.atomic
def manual_movement(*, business, user, variant_id, movement_type, quantity, unit_cost=None, note=""):
    """Opening stock, adjustments and damage/rejection written off. Always goes through post_movement."""
    from catalogue.services import require_variant
    from core.services import record_audit
    if movement_type not in (StockMovement.OPENING, StockMovement.ADJUSTMENT, StockMovement.DAMAGE):
        raise DomainError("Unsupported movement type.")
    v = require_variant(business, variant_id)
    quantity = D(quantity)
    if movement_type == StockMovement.DAMAGE:
        quantity = -abs(quantity)
    elif movement_type == StockMovement.OPENING:
        quantity = abs(quantity)
    tracked = v.product.kind != ProductTemplate.FINISHED
    value = None
    if quantity > 0 and tracked:
        if unit_cost is None or D(unit_cost) < 0:
            raise DomainError("Enter the unit cost for stock coming in.")
        value = r2(quantity * D(unit_cost))
    if not (note or "").strip():
        raise DomainError("A note/reason is required for manual stock movements.")
    mv = post_movement(business=business, variant=v, movement_type=movement_type, quantity=quantity, user=user,
                       value=value, source_type="manual", note=note)
    record_audit(business=business, user=user, action=f"stock.{movement_type}", entity="StockMovement",
                 entity_id=mv.pk, after={"sku": v.sku, "qty": quantity, "note": note})
    return mv
