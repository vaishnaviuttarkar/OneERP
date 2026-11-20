from datetime import timedelta

from django.db.models import Count, F, Sum
from django.utils import timezone

from core.models import AuditEvent
from core.utils import ZERO
from finance.models import CashBookEntry, Expense, Receipt, SupplierPayment
from finance.selectors import account_balances, cash_position
from inventory.models import StockBalance, StockMovement
from inventory.selectors import finished_goods_quantity, total_stock_value
from payroll.models import WageStatement
from production.models import (Batch, BatchStage, FinishedGoodsReceipt, MaterialIssue, OverCutVariance,
                               StageOutcome, Worker, WorkEntry)
from production.services import final_measured_stage, stage_totals
from purchasing.selectors import bills_with_outstanding, total_payable
from sales.selectors import invoices_with_outstanding, total_receivable


def batch_summary(batch):
    final = final_measured_stage(batch)
    planned = batch.planned_qty
    completed = rejected = rework_pending = ZERO
    variants = [p.variant for p in batch.plans.select_related("variant")]
    for st in batch.stages.exclude(outcome_source="none"):
        for v in variants:
            t = stage_totals(st, v)
            rejected += t["rejected"]
            rework_pending += t["rework_sent"] - t["rework_done"]
            if final and st.pk == final.pk:
                completed += t["good"]
    received = FinishedGoodsReceipt.objects.filter(batch=batch).aggregate(t=Sum("quantity"))["t"] or ZERO
    pending = max(ZERO, planned - completed - rejected)
    last = StageOutcome.objects.filter(batch_stage__batch=batch).order_by("-batch_stage__sequence").values_list(
        "batch_stage__stage_name", flat=True).first()
    progress = 100 if batch.status == "completed" else (int(min(completed / planned * 100, 100)) if planned else 0)
    return {"planned": planned, "completed": completed, "pending": pending, "rejected": rejected,
            "rework_pending": rework_pending, "received": received, "progress": progress,
            "current_stage": last or (batch.stages.first().stage_name if batch.stages.exists() else "-"),
            "delayed": bool(batch.expected_completion and batch.expected_completion < timezone.localdate()
                            and batch.status in ("planned", "in_progress"))}


def stage_progress(batch):
    """Per snapshotted stage: good output vs plan and a state (done / current / upcoming) for the workflow stepper."""
    from production.models import OUTCOME_SOURCES
    labels = dict(OUTCOME_SOURCES)
    planned = batch.planned_qty or ZERO
    stages = list(batch.stages.order_by("sequence"))
    goods = {r["batch_stage_id"]: r for r in StageOutcome.objects.filter(batch_stage__batch=batch).order_by()
             .values("batch_stage_id").annotate(good=Sum("good_qty"), processed=Sum("processed_qty"))}
    received = FinishedGoodsReceipt.objects.filter(batch=batch).aggregate(t=Sum("quantity"))["t"] or ZERO
    out, current_found = [], False
    for st in stages:
        if st.outcome_source == "none":
            good = received
        else:
            good = (goods.get(st.pk) or {}).get("good") or ZERO
        pct = int(min(good / planned * 100, 100)) if planned else 0
        done = batch.status == "completed" or (planned and good >= planned)
        if done:
            state = "done"
        elif not current_found and (good > 0 or batch.status != "cancelled"):
            state, current_found = "current", True
        else:
            state = "upcoming"
        out.append({"seq": st.sequence, "name": st.stage_name, "source_label": labels.get(st.outcome_source, st.outcome_source),
                    "good": good, "pct": 100 if batch.status == "completed" else pct, "state": state,
                    "qty_label": (f"{good.normalize():f} / {planned.normalize():f} pcs" + (" received" if st.outcome_source == "none" else " good"))})
    if batch.status == "cancelled":
        for s in out:
            if s["state"] == "current":
                s["state"] = "upcoming"
    return out


def production_insights(business):
    """Batch-status mix, per-stage WIP, client split and active batch progress for the dashboards (quantities only)."""
    from production.models import Batch as B
    all_batches = B.objects.filter(business=business)
    counts = {k: 0 for k, _ in B.STATUSES}
    for row in all_batches.order_by().values("status").annotate(n=Count("id")):
        counts[row["status"]] = row["n"]
    active = list(all_batches.filter(status__in=["planned", "in_progress"]).select_related("product", "client")
                  .order_by("expected_completion", "id"))
    today = timezone.localdate()
    cards, stage_wip, client_mix, delayed, due_week = [], {}, {}, 0, 0
    for b in active:
        s = batch_summary(b)
        delayed += 1 if s["delayed"] else 0
        if b.expected_completion and today <= b.expected_completion <= today + timedelta(days=7):
            due_week += 1
        days_left = (b.expected_completion - today).days if b.expected_completion else None
        cards.append({"batch": b, "s": s, "days_left": days_left})
        stage_wip[s["current_stage"]] = stage_wip.get(s["current_stage"], 0) + 1
        key = b.client.name if b.client else "No client"
        client_mix[key] = client_mix.get(key, ZERO) + b.planned_qty
    completed_30 = all_batches.filter(status="completed", actual_completion__gte=today - timedelta(days=30)).count()
    on_time = all_batches.filter(status="completed", actual_completion__isnull=False, expected_completion__isnull=False,
                                 actual_completion__lte=F("expected_completion")).count()
    with_due = all_batches.filter(status="completed", actual_completion__isnull=False, expected_completion__isnull=False).count()
    status_labels = dict(B.STATUSES)
    return {
        "counts": counts, "active_count": len(active), "delayed": delayed, "due_week": due_week, "completed_30": completed_30,
        "on_time_pct": int(on_time / with_due * 100) if with_due else None,
        "cards": cards[:8],
        "status_chart": {"labels": [status_labels[k] for k in counts], "values": [counts[k] for k in counts]},
        "stage_chart": {"labels": list(stage_wip), "values": list(stage_wip.values())},
        "client_chart": {"labels": list(client_mix), "values": [float(v) for v in client_mix.values()]},
    }


def stage_output_rows(batch):
    rows = []
    for st in batch.stages.all():
        for p in batch.plans.select_related("variant"):
            t = stage_totals(st, p.variant)
            if st.outcome_source != "none":
                rows.append({"stage": st.stage_name, "variant": p.variant, "source": st.outcome_source, **t})
    return rows


def wages_payable(business):
    return sum((s.outstanding for s in WageStatement.objects.filter(business=business, status="finalized")), ZERO)


def _pending_total(business):
    return sum((batch_summary(b)["pending"] for b in Batch.objects.filter(business=business, status__in=["planned", "in_progress"])), ZERO)


def dashboard(business):
    """Owner / accounts dashboard: production insights. Money lives on the Finance overview page."""
    ins = production_insights(business)
    return {
        "active_batches": ins["active_count"], "pending_production": _pending_total(business), "fg_stock": finished_goods_quantity(business),
        "delayed": ins["delayed"], "due_week": ins["due_week"], "completed_30": ins["completed_30"], "on_time_pct": ins["on_time_pct"],
        "ins": ins, "recent": AuditEvent.objects.filter(business=business).select_related("user")[:8],
    }


def production_dashboard(business):
    ins = production_insights(business)
    return {"active_batches": ins["active_count"], "pending_production": _pending_total(business),
            "fg_stock": finished_goods_quantity(business), "delayed": ins["delayed"], "due_week": ins["due_week"],
            "completed_30": ins["completed_30"], "on_time_pct": ins["on_time_pct"], "ins": ins,
            "recent": AuditEvent.objects.filter(business=business, entity__in=["Batch", "WorkEntry", "MaterialIssue", "StageOutcome", "FinishedGoodsReceipt"])[:8]}


def cash_flow(business, start, end):
    qs = CashBookEntry.objects.filter(business=business, entry_date__gte=start, entry_date__lte=end)
    rows = []
    sources = [("Customer receipts", "receipt", "in"), ("Supplier payments", "supplier_payment", "out"),
               ("Expenses", "expense", "out"), ("Wage payments", "wage_payment", "out"),
               ("Worker advances", "worker_advance", "out")]
    for label, field, direction in sources:
        amt = qs.filter(**{f"{field}__isnull": False}).aggregate(t=Sum("amount"))["t"] or ZERO
        rows.append((label, direction, amt))
    inn = sum((a for _, d, a in rows if d == "in"), ZERO)
    out = sum((a for _, d, a in rows if d == "out"), ZERO)
    return rows, inn, out


def daily_cash(business, days=14):
    """Cash in/out per day for the dashboard chart (real cashbook data)."""
    end = timezone.localdate()
    start = end - timedelta(days=days - 1)
    rows = (CashBookEntry.objects.filter(business=business, entry_date__gte=start, entry_date__lte=end)
            .order_by().values("entry_date", "direction").annotate(t=Sum("amount")))
    by = {(r["entry_date"], r["direction"]): r["t"] for r in rows}
    labels, ins, outs = [], [], []
    for i in range(days):
        d = start + timedelta(days=i)
        labels.append(d.strftime("%d %b"))
        ins.append(float(by.get((d, "in"), 0)))
        outs.append(float(by.get((d, "out"), 0)))
    return {"labels": labels, "ins": ins, "outs": outs}


def _ageing(rows, date_attr, today):
    """Bucket open documents (with .outstanding) by age in days."""
    buckets = [("0-30 days", 0, 30), ("31-60 days", 31, 60), ("61-90 days", 61, 90), ("90+ days", 91, 10 ** 6)]
    out = {label: ZERO for label, _, _ in buckets}
    for r in rows:
        age = (today - getattr(r, date_attr)).days
        for label, lo, hi in buckets:
            if lo <= age <= hi:
                out[label] += r.outstanding
                break
    return out


def monthly_cash(business, months=6):
    """Cash in vs out per calendar month (cashbook), oldest month first."""
    from datetime import date
    from django.db.models.functions import TruncMonth
    today = timezone.localdate()
    first = []
    y, m = today.year, today.month
    for _ in range(months):
        first.insert(0, date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    rows = (CashBookEntry.objects.filter(business=business, entry_date__gte=first[0]).annotate(mo=TruncMonth("entry_date"))
            .order_by().values("mo", "direction").annotate(t=Sum("amount")))
    by = {(r["mo"].strftime("%Y-%m"), r["direction"]): r["t"] for r in rows}
    keys = [d.strftime("%Y-%m") for d in first]
    return {"labels": [d.strftime("%b %y") for d in first],
            "ins": [float(by.get((k, "in"), 0)) for k in keys], "outs": [float(by.get((k, "out"), 0)) for k in keys]}



def finance_overview(business):
    """Everything on the Finance overview page. Every figure comes from posted records."""
    from sales.models import SalesInvoice
    today = timezone.localdate()
    month_start = today.replace(day=1)
    open_inv = invoices_with_outstanding(business, only_open=True)
    open_bills = bills_with_outstanding(business, only_open=True)
    receivable = sum((i.outstanding for i in open_inv), ZERO)
    payable = sum((b.outstanding for b in open_bills), ZERO)
    wages = wages_payable(business)
    cash = cash_position(business)
    stock = total_stock_value(business) or ZERO
    month_cash = CashBookEntry.objects.filter(business=business, entry_date__gte=month_start)
    cash_in_m = month_cash.filter(direction="in").aggregate(t=Sum("amount"))["t"] or ZERO
    cash_out_m = month_cash.filter(direction="out").aggregate(t=Sum("amount"))["t"] or ZERO
    sales_m = SalesInvoice.objects.filter(business=business, status="posted", invoice_date__gte=month_start).aggregate(t=Sum("total"))["t"] or ZERO
    exp_m = Expense.objects.filter(business=business, expense_date__gte=month_start).aggregate(t=Sum("amount"))["t"] or ZERO
    by_cat = {}
    for r in Expense.objects.filter(business=business, expense_date__gte=today - timedelta(days=90)).order_by().values("category").annotate(t=Sum("amount")):
        by_cat[dict(Expense.CATEGORIES).get(r["category"], r["category"])] = float(r["t"])
    debtors, creditors = {}, {}
    for i in open_inv:
        debtors[(i.customer_id, i.customer.name)] = debtors.get((i.customer_id, i.customer.name), ZERO) + i.outstanding
    for x in open_bills:
        creditors[(x.supplier_id, x.supplier.name)] = creditors.get((x.supplier_id, x.supplier.name), ZERO) + x.outstanding
    unpaid_wages = [s for s in WageStatement.objects.filter(business=business, status="finalized").select_related("worker").order_by("period_end")
                    if s.outstanding > 0]
    return {
        "stock_value": stock, "receivable": receivable, "payable": payable, "wages_payable": wages, "cash_position": cash,
        "net_working": cash + receivable + stock - payable - wages,
        "cash_in_month": cash_in_m, "cash_out_month": cash_out_m, "net_month": cash_in_m - cash_out_m,
        "sales_month": sales_m, "expenses_month": exp_m, "accounts": account_balances(business),
        "open_invoices": len(open_inv), "open_bills": len(open_bills), "unpaid_wage_count": len(unpaid_wages),
        "overdue_invoices": sum(1 for i in open_inv if (today - i.invoice_date).days > 30),
        "rec_ageing": _ageing(open_inv, "invoice_date", today), "pay_ageing": _ageing(open_bills, "bill_date", today),
        "top_debtors": sorted(debtors.items(), key=lambda kv: -kv[1])[:5],
        "top_creditors": sorted(creditors.items(), key=lambda kv: -kv[1])[:5],
        "unpaid_wages": unpaid_wages[:5],
        "chart": {   # display-only float conversion; every figure comes from the database
            "cash": daily_cash(business, 14),
            "mix": {"labels": ["Cash & bank", "Customers owe you", "You owe suppliers", "Wages payable"],
                    "values": [max(float(cash), 0), float(receivable), float(payable), float(wages)]},
            "monthly": monthly_cash(business, 6),
            "expenses": {"labels": list(by_cat), "values": list(by_cat.values())},
        },
    }

