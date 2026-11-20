"""Read-only reconciliation. Finds inconsistencies; NEVER repairs data."""
from dataclasses import dataclass, field

from django.db.models import Count, F, Q, Sum

from core.utils import ZERO, r2
from finance.models import CashBookEntry, Expense, Receipt, ReceiptAllocation, SupplierPayment, SupplierPaymentAllocation
from inventory.models import StockBalance, StockMovement
from payroll.models import WagePayment, WageStatement
from production.models import (Batch, BatchStage, FinishedGoodsReceipt, ReworkLink, StageOutcome, WorkEntry)
from production.services import available_input, final_measured_stage, stage_fingerprint, stage_totals
from purchasing.models import PurchaseBill
from sales.models import SalesInvoice


@dataclass
class Check:
    group: str
    name: str
    issues: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.issues


def _sum(qs, f):
    return qs.aggregate(t=Sum(f))["t"] or ZERO


def inventory_quantity(b):
    c = Check("Inventory", "Stock balance equals stock journal")
    sums = {r["variant"]: r["t"] for r in StockMovement.objects.filter(business=b).values("variant").annotate(t=Sum("quantity"))}
    bals = {x.variant_id: x for x in StockBalance.objects.filter(business=b)}
    for vid in set(sums) | set(bals):
        have = bals[vid].quantity if vid in bals else ZERO
        if have != sums.get(vid, ZERO):
            c.issues.append(f"Variant {vid}: balance {have} vs journal {sums.get(vid, ZERO)}")
    return c


def inventory_value(b):
    c = Check("Inventory", "Stock value equals movement value")
    sums = {r["variant"]: r["t"] for r in StockMovement.objects.filter(business=b).values("variant").annotate(t=Sum("value"))}
    for x in StockBalance.objects.filter(business=b):
        if x.value != sums.get(x.variant_id, ZERO):
            c.issues.append(f"Variant {x.variant_id}: value {x.value} vs journal {sums.get(x.variant_id, ZERO)}")
    return c


def supplier_allocations(b):
    c = Check("Purchasing", "Supplier bill and payment allocations")
    for bill in PurchaseBill.objects.filter(business=b, status="posted").annotate(a=Sum("payment_allocations__amount")):
        if (bill.a or ZERO) > bill.total:
            c.issues.append(f"Bill {bill.number}: allocated {bill.a} > total {bill.total}")
    for p in SupplierPayment.objects.filter(business=b).annotate(a=Sum("allocations__amount")):
        if (p.a or ZERO) > p.amount:
            c.issues.append(f"Payment {p.number}: allocated {p.a} > amount {p.amount}")
    return c


def invoice_allocations(b):
    c = Check("Sales", "Sales invoice and receipt allocations")
    for inv in SalesInvoice.objects.filter(business=b).annotate(a=Sum("receipt_allocations__amount")):
        if (inv.a or ZERO) > inv.total:
            c.issues.append(f"Invoice {inv.number}: allocated {inv.a} > total {inv.total}")
        if inv.status == "cancelled" and inv.a:
            c.issues.append(f"Invoice {inv.number}: cancelled but has allocations")
        if inv.status == "posted" and inv.total != inv.total.to_integral_value():
            c.issues.append(f"Invoice {inv.number}: total {inv.total} is not a whole rupee")
    for r in Receipt.objects.filter(business=b).annotate(a=Sum("allocations__amount")):
        if (r.a or ZERO) > r.amount:
            c.issues.append(f"Receipt {r.number}: allocated {r.a} > amount {r.amount}")
    return c


def challan_invoicing(b):
    c = Check("Sales", "Challan dispatch vs invoiced quantity")
    from sales.models import DeliveryChallanLine
    from sales.services import active_invoiced_qty
    for cl in DeliveryChallanLine.objects.filter(business=b, challan__status="posted"):
        if active_invoiced_qty(cl) > cl.quantity:
            c.issues.append(f"Challan line {cl.pk}: invoiced exceeds dispatched")
        if not cl.movement_id:
            c.issues.append(f"Challan line {cl.pk}: posted without a dispatch movement")
    return c


def production_quantities(b):
    c = Check("Production", "Production quantities and outcomes")
    for o in StageOutcome.objects.filter(business=b):
        parts = o.good_qty + o.rejected_worker_qty + o.rejected_other_qty + o.rework_worker_qty + o.rework_other_qty
        if parts != o.processed_qty:
            c.issues.append(f"Outcome {o.pk}: processed {o.processed_qty} != parts {parts}")
    for e in WorkEntry.objects.filter(business=b, is_manual=False, op_is_payroll_only=False):
        if not hasattr(e, "outcome"):
            c.issues.append(f"Work entry {e.pk}: counting entry without a stage outcome")
    for e in WorkEntry.objects.filter(business=b, op_is_payroll_only=True):
        if hasattr(e, "outcome"):
            c.issues.append(f"Work entry {e.pk}: payroll-only entry created a production outcome")
    for e in WorkEntry.objects.filter(business=b, is_manual=True):
        if hasattr(e, "outcome"):
            c.issues.append(f"Work entry {e.pk}: manual entry created a production outcome")
    for bs in BatchStage.objects.filter(business=b).exclude(outcome_source="none").select_related("batch"):
        for plan in bs.batch.plans.all():
            avail = available_input(bs, plan.variant)
            if avail is not None and avail < 0:
                c.issues.append(f"Batch {bs.batch.number} {bs.stage_name}/{plan.variant.sku}: processed exceeds input")
    return c


def rework(b):
    c = Check("Production", "Rework links and quantities")
    for e in WorkEntry.objects.filter(business=b, op_is_rework=False, is_manual=False):
        sent = e.rework_worker_qty + e.rework_other_qty
        linked = _sum(ReworkLink.objects.filter(source_entry=e), "quantity")
        if linked > sent:
            c.issues.append(f"Entry {e.pk}: linked {linked} > sent to rework {sent}")
    for e in WorkEntry.objects.filter(business=b, op_is_rework=True):
        linked = _sum(ReworkLink.objects.filter(rework_entry=e), "quantity")
        if linked != e.processed_qty:
            c.issues.append(f"Rework entry {e.pk}: linked {linked} != completed {e.processed_qty}")
    return c


def finished_goods(b):
    c = Check("Production", "Finished goods receipts")
    for batch in Batch.objects.filter(business=b):
        final = final_measured_stage(batch)
        for p in batch.plans.select_related("variant"):
            got = _sum(FinishedGoodsReceipt.objects.filter(batch=batch, variant=p.variant), "quantity")
            good = stage_totals(final, p.variant)["good"] if final else ZERO
            if got > good:
                c.issues.append(f"Batch {batch.number}/{p.variant.sku}: received {got} > good output {good}")
    for r in FinishedGoodsReceipt.objects.filter(business=b).select_related("movement"):
        if r.movement.quantity != r.quantity:
            c.issues.append(f"FG receipt {r.pk}: movement quantity differs")
    return c


def payroll(b):
    c = Check("Payroll", "Work entries and wage statements")
    for st in WageStatement.objects.filter(business=b):
        lines = _sum(st.lines, "amount")
        if st.status == "finalized":
            if lines != st.gross_earnings:
                c.issues.append(f"{st.number}: lines {lines} != gross {st.gross_earnings}")
            if st.gross_earnings + st.additional_earnings - st.deductions - st.advance_recovery != st.net_payable:
                c.issues.append(f"{st.number}: net payable arithmetic mismatch")
    for e in WorkEntry.objects.filter(business=b, wage_line__isnull=False).select_related("wage_line"):
        if e.wage_line.amount != e.earnings:
            c.issues.append(f"Work entry {e.pk}: wage line amount differs from entry earnings")
    for e in WorkEntry.objects.filter(business=b):
        if r2(e.paid_qty * e.rate) != e.earnings:
            c.issues.append(f"Work entry {e.pk}: earnings != paid qty x rate snapshot")
    return c


def wage_payments(b):
    c = Check("Payroll", "Wage payment balances")
    for st in WageStatement.objects.filter(business=b, status="finalized").annotate(a=Sum("payments__applied_amount")):
        if (st.a or ZERO) != st.paid_amount:
            c.issues.append(f"{st.number}: paid_amount {st.paid_amount} != applied payments {st.a or ZERO}")
    for p in WagePayment.objects.filter(business=b):
        if not hasattr(p, "cashbook_entry"):
            c.issues.append(f"Wage payment {p.pk}: no cashbook entry")
    return c


def cashbook(b):
    c = Check("Cash", "Cashbook consistency")
    for model, label in ((Receipt, "Receipt"), (SupplierPayment, "Supplier payment"), (Expense, "Expense")):
        for obj in model.objects.filter(business=b):
            cb = getattr(obj, "cashbook_entry", None)
            if cb is None:
                c.issues.append(f"{label} {obj.number}: no cashbook entry")
            elif cb.amount != obj.amount:
                c.issues.append(f"{label} {obj.number}: cashbook amount {cb.amount} != {obj.amount}")
    for p in WagePayment.objects.filter(business=b):
        cb = getattr(p, "cashbook_entry", None)
        if cb is not None and cb.amount != p.amount:
            c.issues.append(f"Wage payment {p.pk}: cashbook amount differs")
    return c


def snapshots(b):
    c = Check("Production", "Historical snapshots versus configuration")
    for batch in Batch.objects.filter(business=b):
        fp = stage_fingerprint([(s.stage_id, s.sequence, s.outcome_source) for s in batch.stages.all()])
        if fp != batch.workflow_fingerprint:
            c.issues.append(f"Batch {batch.number}: stage snapshot does not match its stored fingerprint")
    return c


ALL_CHECKS = [inventory_quantity, inventory_value, supplier_allocations, invoice_allocations, challan_invoicing,
              production_quantities, rework, finished_goods, payroll, wage_payments, cashbook, snapshots]


def run_all(business):
    return [fn(business) for fn in ALL_CHECKS]
