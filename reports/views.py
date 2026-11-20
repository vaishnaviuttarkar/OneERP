from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils import timezone

from accounts.permissions import ACCOUNTS, OWNER, PRODUCTION, has_role, role_required
from catalogue.models import ProductVariant
from core.services import get_current_business
from core.viewkit import inr, link, qf, render_list
from finance.models import CashBookEntry, Expense, Receipt, SupplierPayment
from finance.selectors import account_balances
from inventory.models import StockBalance, StockMovement
from inventory.selectors import low_stock
from payroll.models import WagePayment, WageStatement, WorkerAdvance
from payroll.services import advance_balance
from production.models import Batch, MaterialIssue, OverCutVariance, StageOutcome, WorkEntry, Worker
from purchasing.selectors import bills_with_outstanding
from sales.models import DeliveryChallan
from sales.selectors import invoices_with_outstanding
from . import reconciliation, selectors

M = (OWNER, ACCOUNTS)
P = (OWNER, ACCOUNTS, PRODUCTION)


def r_valuation(b, g):
    rows = [[s.variant.label, s.variant.product.get_kind_display(), qf(s.quantity), inr(s.value)] for s in StockBalance.objects.filter(business=b).select_related("variant__product")]
    return ["Item", "Kind", "Qty", "Value"], rows


def r_lowstock(b, g):
    th = b.settings.low_stock_threshold
    return ["Item", "On hand"], [[s.variant.label, qf(s.quantity)] for s in low_stock(b, th)]


def r_consumption(b, g):
    rows = [[m.issue_date, m.batch.number, m.variant.label, qf(m.quantity), inr(m.value)] for m in MaterialIssue.objects.filter(business=b).select_related("batch", "variant__product")]
    return ["Date", "Batch", "Material", "Qty", "Cost"], rows


def r_adjustments(b, g):
    rows = [[m.business_date, m.variant.label, m.get_movement_type_display(), qf(m.quantity), inr(m.value), m.note]
            for m in StockMovement.objects.filter(business=b, movement_type__in=["adjustment", "damage", "opening"]).select_related("variant__product")]
    return ["Date", "Item", "Type", "Qty", "Value", "Note"], rows


def r_batches(b, g):
    rows = []
    for x in Batch.objects.filter(business=b).select_related("product"):
        s = selectors.batch_summary(x)
        rows.append([x.number, x.product.name, x.status, qf(s["planned"]), qf(s["completed"]), qf(s["pending"]), qf(s["rejected"]), qf(s["rework_pending"]), "Yes" if s["delayed"] else ""])
    return ["Batch", "Product", "Status", "Planned", "Completed", "Pending", "Rejected", "Rework pending", "Delayed"], rows


def r_stage_output(b, g):
    rows = []
    for x in Batch.objects.filter(business=b):
        for r in selectors.stage_output_rows(x):
            rows.append([x.number, r["stage"], r["variant"].sku, qf(r["processed_normal"]), qf(r["good"]), qf(r["rejected"]), qf(r["rework_sent"])])
    return ["Batch", "Stage", "Size", "Processed", "Good", "Rejected", "Rework"], rows


def r_rejections(b, g):
    rows = [[o.outcome_date, o.batch_stage.batch.number, o.batch_stage.stage_name, o.variant.sku, qf(o.rejected_worker_qty), qf(o.rejected_other_qty),
             qf(o.rework_worker_qty + o.rework_other_qty), o.rejection_reason.replace("_", " ")]
            for o in StageOutcome.objects.filter(business=b).exclude(rejected_worker_qty=0, rejected_other_qty=0, rework_worker_qty=0, rework_other_qty=0).select_related("batch_stage__batch", "variant")]
    return ["Date", "Batch", "Stage", "Size", "Rej. worker", "Rej. other", "Rework", "Reason"], rows


def r_overcut(b, g):
    return ["Batch", "Stage", "Size", "Extra qty", "Reason"], [[o.batch.number, o.batch_stage.stage_name, o.variant.sku, qf(o.extra_qty), o.reason] for o in OverCutVariance.objects.filter(business=b).select_related("batch", "batch_stage", "variant")]


def r_worker_earnings(b, g):
    rows = []
    for w in Worker.objects.filter(business=b):
        es = WorkEntry.objects.filter(business=b, worker=w)
        rows.append([w.name, es.count(), inr(sum((e.earnings for e in es), 0)), inr(sum((e.earnings for e in es.filter(wage_line__isnull=True)), 0))])
    return ["Worker", "Entries", "Total earnings", "Not yet on a statement"], rows


def r_wage_statements(b, g):
    return ["No.", "Worker", "Period", "Net", "Paid", "Outstanding"], [[s.number or "draft", s.worker.name, f"{s.period_start} → {s.period_end}", inr(s.net_payable), inr(s.paid_amount), inr(s.outstanding)]
                                                                        for s in WageStatement.objects.filter(business=b).select_related("worker")]


def r_advances(b, g):
    return ["Worker", "Advance balance"], [[w.name, inr(advance_balance(w))] for w in Worker.objects.filter(business=b)]


def r_wage_payments(b, g):
    return ["Date", "Statement", "Cash paid", "Applied", "To advance"], [[p.payment_date, p.statement.number, inr(p.amount), inr(p.applied_amount), inr(p.excess_amount)] for p in WagePayment.objects.filter(business=b).select_related("statement")]


def r_cust_out(b, g):
    return ["Invoice", "Customer", "Date", "Total", "Received", "Outstanding"], [[i.number, i.customer.name, i.invoice_date, inr(i.total), inr(i.received), inr(i.outstanding)] for i in invoices_with_outstanding(b, only_open=True)]


def r_receipts(b, g):
    return ["Receipt", "Customer", "Date", "Amount"], [[r.number, r.customer.name, r.receipt_date, inr(r.amount)] for r in Receipt.objects.filter(business=b).select_related("customer")]


def r_sales(b, g):
    return ["Invoice", "Customer", "Date", "Total"], [[i.number, i.customer.name, i.invoice_date, inr(i.total)] for i in invoices_with_outstanding(b)]


def r_challans(b, g):
    return ["Challan", "Customer", "Date", "Status"], [[c.number or "draft", c.customer.name, c.challan_date, c.status] for c in DeliveryChallan.objects.filter(business=b).select_related("customer")]


def r_purchases(b, g):
    return ["Bill", "Supplier", "Date", "Total"], [[x.number, x.supplier.name, x.bill_date, inr(x.total)] for x in bills_with_outstanding(b)]


def r_supp_out(b, g):
    return ["Bill", "Supplier", "Date", "Total", "Paid", "Outstanding"], [[x.number, x.supplier.name, x.bill_date, inr(x.total), inr(x.paid), inr(x.outstanding)] for x in bills_with_outstanding(b, only_open=True)]


def r_payments(b, g):
    return ["Payment", "Supplier", "Date", "Amount"], [[p.number, p.supplier.name, p.payment_date, inr(p.amount)] for p in SupplierPayment.objects.filter(business=b).select_related("supplier")]


def r_expenses(b, g):
    return ["No.", "Date", "Category", "Amount"], [[e.number, e.expense_date, e.get_category_display(), inr(e.amount)] for e in Expense.objects.filter(business=b)]


def r_balances(b, g):
    return ["Account", "In", "Out", "Balance"], [[r["account"].name, inr(r["in"]), inr(r["out"]), inr(r["balance"])] for r in account_balances(b)]


def r_cashflow(b, g):
    start = g.get("from") or str(timezone.localdate() - timedelta(days=30))
    end = g.get("to") or str(timezone.localdate())
    rows, inn, out = selectors.cash_flow(b, start, end)
    data = [[l, inr(a) if d == "in" else "", inr(a) if d == "out" else ""] for l, d, a in rows] + [["Net cash flow " + f"({start} to {end})", inr(inn), inr(out)], ["", "Net", inr(inn - out)]]
    return ["Source", "Cash in", "Cash out"], data


REPORTS = {
    "stock-valuation": ("Stock valuation", M, r_valuation), "low-stock": ("Low stock", P, r_lowstock), "material-consumption": ("Material consumption", M, r_consumption),
    "stock-adjustments": ("Stock adjustments", M, r_adjustments), "batch-status": ("Batch status / planned vs completed", P, r_batches),
    "stage-output": ("Stage-wise output & pending work", P, r_stage_output), "rejections": ("Rejections & rework", P, r_rejections), "over-cutting": ("Over-cutting variance", P, r_overcut),
    "worker-earnings": ("Worker-wise earnings", M, r_worker_earnings), "wage-statements": ("Weekly wage statements & outstanding wages", M, r_wage_statements),
    "worker-advances": ("Worker advances", M, r_advances), "wage-payments": ("Wage payments", M, r_wage_payments),
    "sales": ("Sales invoices", M, r_sales), "customer-outstanding": ("Customer outstanding", M, r_cust_out), "receipt-history": ("Receipt history", M, r_receipts),
    "challans": ("Delivery challans", M, r_challans), "purchases": ("Purchase bills", M, r_purchases), "supplier-outstanding": ("Supplier outstanding", M, r_supp_out),
    "payment-history": ("Supplier payment history", M, r_payments), "expenses": ("Expenses", M, r_expenses), "balances": ("Cash / bank balances", M, r_balances),
    "cash-flow": ("Cash flow", M, r_cashflow),
}


@role_required(*P)
def index(request):
    items = [(slug, t) for slug, (t, roles, _) in REPORTS.items() if has_role(request.user, roles)]
    return render(request, "reports_index.html", {"items": items})


@role_required(*P)
def report(request, slug):
    if slug not in REPORTS:
        from django.http import Http404
        raise Http404
    title, roles, fn = REPORTS[slug]
    if not has_role(request.user, roles):
        raise PermissionDenied("Your role cannot view this report.")
    headers, rows = fn(get_current_business(), request.GET)
    return render_list(request, title=title, headers=headers, rows=rows, search=False, per_page=50)


@role_required(OWNER)
def reconciliation_view(request):
    checks = reconciliation.run_all(get_current_business())
    return render(request, "reconciliation.html", {"checks": checks, "failed": sum(1 for c in checks if not c.ok)})
