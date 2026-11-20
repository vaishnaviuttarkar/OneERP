from django import forms
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import ACCOUNTS, OWNER, role_required
from core.forms import BootstrapMixin, DateInput
from core.models import FinancialAccount
from core.services import get_current_business
from core.utils import PAYMENT_STATUSES, payment_status
from core.viewkit import badge, detail_page, form_page, handle_post, inr, link, payment_badge, post_btn_csrf, qf, render_list, row_actions, search_filter
from django.shortcuts import render
from production.models import StageOutcome, Worker
from . import services
from .models import WageAdjustment, WageStatement, WorkerAdvance

ROLES = (OWNER, ACCOUNTS)


class StatementForm(BootstrapMixin, forms.Form):
    worker = forms.ModelChoiceField(queryset=Worker.objects.none())
    period_start = forms.DateField(widget=DateInput())
    period_end = forms.DateField(widget=DateInput())

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["worker"].queryset = Worker.objects.filter(business=business)


class AdjustForm(BootstrapMixin, forms.Form):
    kind = forms.ChoiceField(choices=WageAdjustment.KINDS)
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    reason = forms.CharField(max_length=300)
    rejection_outcome = forms.ModelChoiceField(queryset=StageOutcome.objects.none(), required=False,
                                               label="Rejection reference (required for deductions)")

    def __init__(self, *a, business=None, worker=None, **k):
        super().__init__(*a, **k)
        qs = StageOutcome.objects.filter(business=business, work_entry__worker=worker, rejected_worker_qty__gt=0).select_related("batch_stage__batch", "work_entry")
        self.fields["rejection_outcome"].queryset = qs
        self.fields["rejection_outcome"].label_from_instance = lambda o: f"{o.batch_stage.batch.number} {o.batch_stage.stage_name} {o.outcome_date}: {o.rejected_worker_qty} worker rejects"


class FinalizeForm(BootstrapMixin, forms.Form):
    advance_recovery = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, initial=0)


class PayForm(BootstrapMixin, forms.Form):
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    account = forms.ModelChoiceField(queryset=FinancialAccount.objects.none())
    payment_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["account"].queryset = FinancialAccount.objects.filter(business=business, is_active=True)


class AdvanceForm(PayForm):
    worker = forms.ModelChoiceField(queryset=Worker.objects.none())

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, business=business, **k)
        self.fields["worker"].queryset = Worker.objects.filter(business=business, is_active=True)


def _pay_state(s):
    """Payment status of a wage statement: drafts are not payable yet."""
    if s.status != WageStatement.FINALIZED:
        return "draft"
    return payment_status(s.net_payable, s.paid_amount)


@role_required(*ROLES)
def statements(request):
    b = get_current_business()
    qs = search_filter(WageStatement.objects.filter(business=b).select_related("worker"), request.GET.get("q", "").strip(), "number", "worker__name")
    pay = request.GET.get("payment", "")
    st = request.GET.get("status", "")
    if st in dict(WageStatement.STATUSES):
        qs = qs.filter(status=st)
    rows = []
    for s in qs:
        state = _pay_state(s)
        pay_cell = badge("Draft") if state == "draft" else payment_badge(state)
        if pay and state != pay:
            continue
        acts = row_actions(request, edit=None, delete=f"/payroll/{s.pk}/delete/" if s.status == WageStatement.DRAFT else None,
                           confirm=f"Delete draft statement for {s.worker.name}?")
        rows.append([link(f"/payroll/{s.pk}/", s.number or f"Draft #{s.pk}"), s.worker.name, f"{s.period_start} → {s.period_end}", inr(s.gross_earnings),
                     inr(s.net_payable), inr(s.paid_amount), inr(s.outstanding) if s.status == "finalized" else "-", badge(s.status), pay_cell, acts])
    return render_list(request, title="Weekly wage statements",
                       headers=["No.", "Worker", "Period", "Gross", "Net", "Paid", "Outstanding", "Status", "Payment", ""], rows=rows,
                       add_url="/payroll/new/", add_label="New statement", extra_actions=[("Advances", "/payroll/advances/")],
                       filters=[{"name": "status", "label": "Status", "current": st, "options": WageStatement.STATUSES},
                                {"name": "payment", "label": "Payment", "current": pay, "options": PAYMENT_STATUSES}])


@role_required(*ROLES)
def statement_create(request):
    b = get_current_business()
    form = StatementForm(request.POST or None, business=b)
    return form_page(request, form, "New wage statement",
                     lambda f, fs: services.create_statement(business=b, user=request.user, worker_id=f.cleaned_data["worker"].pk,
                                                             period_start=f.cleaned_data["period_start"], period_end=f.cleaned_data["period_end"]),
                     lambda s: f"/payroll/{s.pk}/", back="/payroll/",
                     note="Attaches all of the worker's unattached work entries dated in the period (max 7 days). Attached entries become locked.")


@role_required(*ROLES)
def statement_detail(request, pk):
    b = get_current_business()
    st = get_object_or_404(WageStatement.objects.select_related("worker"), pk=pk, business=b)
    lines = [[l.work_entry.work_date, l.work_entry.op_name, qf(l.work_entry.paid_qty), inr(l.work_entry.rate), inr(l.amount)]
             for l in st.lines.select_related("work_entry")]
    adj_rows = []
    for a in st.adjustments.select_related("proposed_by"):
        actions = ""
        if a.status == "proposed" and request.user.is_owner:
            actions = post_btn_csrf(request, f"/payroll/adjustments/{a.pk}/approve/", "Approve", "Approve this adjustment?", "btn-success") + \
                      post_btn_csrf(request, f"/payroll/adjustments/{a.pk}/reject/", "Reject", "Reject this adjustment?", "btn-outline-danger")
        flag = " (self-approved)" if hasattr(a, "decision") and a.decision.is_self_approval else ""
        adj_rows.append([a.get_kind_display(), inr(a.amount), a.reason, a.proposed_by.username, badge(a.status) , flag, actions])
    pays = [[p.payment_date, inr(p.amount), inr(p.applied_amount), inr(p.excess_amount), p.account.name] for p in st.payments.select_related("account")]
    buttons = []
    if st.status == "draft":
        buttons = [link(f"/payroll/{st.pk}/adjust/", "Propose adjustment"), " ", link(f"/payroll/{st.pk}/finalize/", "Finalize…"), " ",
                   post_btn_csrf(request, f"/payroll/{st.pk}/delete/", "Delete draft", "Delete this draft statement?", "btn-outline-danger")]
    else:
        buttons = [link(f"/payroll/{st.pk}/pay/", "Record payment")]
    state = _pay_state(st)
    pay_badge = badge("Draft") if state == "draft" else payment_badge(state)
    return detail_page(request, title=st.number or f"Draft statement #{st.pk}", badge_html=f"{badge(st.status)} {pay_badge}",
                       facts=[("Worker", st.worker.name), ("Period", f"{st.period_start} → {st.period_end}"), ("Gross", inr(st.gross_earnings)),
                              ("Additional earnings", inr(st.additional_earnings)), ("Deductions", inr(st.deductions)), ("Advance recovered", inr(st.advance_recovery)),
                              ("Net payable", inr(st.net_payable)), ("Paid / outstanding", f"{inr(st.paid_amount)} / {inr(st.outstanding)}"),
                              ("Worker advance balance", inr(services.advance_balance(st.worker)))],
                       sections=[{"title": "Work entries", "headers": ["Date", "Operation", "Paid qty", "Rate", "Amount"], "rows": lines},
                                 {"title": "Adjustments", "headers": ["Type", "Amount", "Reason", "Proposed by", "Status", "", ""], "rows": adj_rows},
                                 {"title": "Payments", "headers": ["Date", "Cash paid", "Applied to wages", "To advance", "Account"], "rows": pays}],
                       buttons=buttons, print_url=f"/payroll/{st.pk}/print/", back="/payroll/")


@role_required(*ROLES)
def adjust_new(request, pk):
    b = get_current_business()
    st = get_object_or_404(WageStatement, pk=pk, business=b)
    form = AdjustForm(request.POST or None, business=b, worker=st.worker)

    def handler(f, fs):
        d = f.cleaned_data
        return services.propose_adjustment(business=b, user=request.user, statement_id=st.pk, kind=d["kind"], amount=d["amount"], reason=d["reason"],
                                           rejection_outcome_id=d["rejection_outcome"].pk if d["rejection_outcome"] else None)
    return form_page(request, form, "Propose wage adjustment", handler, f"/payroll/{st.pk}/", back=f"/payroll/{st.pk}/",
                     note="Additional earnings and deductions need a reason and Owner approval. Deductions need an identified worker and a rejection reference.")


@role_required(OWNER)
@require_POST
def adjust_decide(request, pk, decision):
    b = get_current_business()
    a = get_object_or_404(WageAdjustment, pk=pk, business=b)
    return handle_post(request, lambda: services.decide_adjustment(business=b, user=request.user, adjustment_id=pk, approve=decision == "approve"),
                       f"/payroll/{a.statement_id}/", "Decision recorded.")


@role_required(*ROLES)
def finalize(request, pk):
    b = get_current_business()
    st = get_object_or_404(WageStatement, pk=pk, business=b)
    form = FinalizeForm(request.POST or None)
    return form_page(request, form, "Finalize wage statement",
                     lambda f, fs: services.finalize_statement(business=b, user=request.user, statement_id=st.pk, advance_recovery=f.cleaned_data["advance_recovery"] or 0),
                     lambda s: f"/payroll/{s.pk}/", back=f"/payroll/{st.pk}/", submit="Finalize (irreversible)",
                     note=f"Finalized statements are immutable. Worker advance balance: {inr(services.advance_balance(st.worker))}.")


@role_required(*ROLES)
def pay(request, pk):
    b = get_current_business()
    st = get_object_or_404(WageStatement, pk=pk, business=b)
    form = PayForm(request.POST or None, business=b, initial={"amount": st.outstanding})
    return form_page(request, form, f"Pay {st.number}",
                     lambda f, fs: services.pay_statement(business=b, user=request.user, statement_id=st.pk, amount=f.cleaned_data["amount"],
                                                          account_id=f.cleaned_data["account"].pk, payment_date=f.cleaned_data["payment_date"]),
                     f"/payroll/{st.pk}/", back=f"/payroll/{st.pk}/", note=f"Outstanding: {inr(st.outstanding)}. Any excess is recorded as a worker advance.")


@role_required(*ROLES)
@require_POST
def delete_draft(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_draft_statement(business=b, user=request.user, statement_id=pk), "/payroll/", "Draft deleted.")


@role_required(*ROLES)
def advances(request):
    b = get_current_business()
    rows = [[a.business_date, a.worker.name, a.get_direction_display(), a.source.replace("_", " "), inr(a.amount)]
            for a in WorkerAdvance.objects.filter(business=b).select_related("worker").order_by("-id")[:300]]
    bal = [f"{w.name}: {inr(services.advance_balance(w))}" for w in Worker.objects.filter(business=b) if services.advance_balance(w)]
    return render_list(request, title="Worker advances", headers=["Date", "Worker", "Direction", "Source", "Amount"], rows=rows, add_url="/payroll/advances/new/",
                       add_label="Issue advance", search=False, subtitle="Balances - " + (", ".join(bal) or "none outstanding"))


@role_required(*ROLES)
def advance_new(request):
    b = get_current_business()
    form = AdvanceForm(request.POST or None, business=b)
    return form_page(request, form, "Issue worker advance",
                     lambda f, fs: services.issue_advance(business=b, user=request.user, worker_id=f.cleaned_data["worker"].pk, amount=f.cleaned_data["amount"],
                                                          account_id=f.cleaned_data["account"].pk, business_date=f.cleaned_data["payment_date"]),
                     "/payroll/advances/", back="/payroll/advances/")


@role_required(*ROLES)
def print_statement(request, pk):
    b = get_current_business()
    st = get_object_or_404(WageStatement.objects.select_related("worker"), pk=pk, business=b)
    return render(request, "print/wage_statement.html", {"business": b, "st": st, "title": "Wage Statement",
                  "lines": st.lines.select_related("work_entry").order_by("work_entry__work_date"), "adjustments": st.adjustments.filter(status="approved")})
