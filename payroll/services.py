from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from core.exceptions import DomainError
from core.services import next_number, record_audit
from core.utils import D, ZERO, r2
from finance.services import add_cashbook, get_account
from production.models import StageOutcome, Worker, WorkEntry
from .models import (WageAdjustment, WageAdjustmentDecision, WagePayment, WageStatement, WageStatementLine,
                     WorkerAdvance)


def advance_balance(worker):
    t = lambda d: WorkerAdvance.objects.filter(worker=worker, direction=d).aggregate(s=Sum("amount"))["s"] or ZERO
    return t("issued") - t("recovered")


@transaction.atomic
def create_statement(*, business, user, worker_id, period_start, period_end):
    """Draft weekly statement. Attaches every unattached work entry of the worker dated in the period."""
    worker = Worker.objects.get(pk=worker_id, business=business)
    if period_end < period_start:
        raise DomainError("Period end is before period start.")
    if (period_end - period_start).days > 6:
        raise DomainError("A wage statement covers at most one week (7 days).")
    try:
        with transaction.atomic():
            st = WageStatement.objects.create(business=business, worker=worker, period_start=period_start,
                                              period_end=period_end, created_by=user)
    except IntegrityError:
        raise DomainError("This period overlaps an existing wage statement for the worker.")
    entries = (WorkEntry.objects.select_for_update(of=("self",))
               .filter(business=business, worker=worker, work_date__gte=period_start, work_date__lte=period_end,
                       wage_line__isnull=True).order_by("id"))
    gross = ZERO
    for e in entries:
        WageStatementLine.objects.create(business=business, statement=st, work_entry=e, amount=e.earnings)
        gross += e.earnings
    if gross == 0 and not entries:
        raise DomainError("No unattached work entries in this period.")
    WageStatement.objects.filter(pk=st.pk).update(gross_earnings=gross, net_payable=gross)
    record_audit(business=business, user=user, action="wage_statement.create", entity="WageStatement",
                 entity_id=st.pk, after={"worker": worker.code, "gross": gross})
    st.refresh_from_db()
    return st


@transaction.atomic
def delete_draft_statement(*, business, user, statement_id):
    st = WageStatement.objects.select_for_update().get(pk=statement_id, business=business)
    if st.status != WageStatement.DRAFT:
        raise DomainError("Finalized wage statements cannot be deleted.")
    if st.adjustments.filter(decision__isnull=False).exists():
        raise DomainError("Statements with decided adjustments cannot be deleted (approval records are immutable).")
    record_audit(business=business, user=user, action="wage_statement.delete_draft", entity="WageStatement",
                 entity_id=st.pk, before={"gross": st.gross_earnings})
    st.lines.all().delete()
    st.adjustments.all().delete()
    WageStatement.objects.filter(pk=st.pk).delete()


@transaction.atomic
def propose_adjustment(*, business, user, statement_id, kind, amount, reason, rejection_outcome_id=None):
    st = WageStatement.objects.select_for_update().get(pk=statement_id, business=business)
    if st.status != WageStatement.DRAFT:
        raise DomainError("Finalized statements are immutable.")
    amount = D(amount)
    if amount <= 0 or not (reason or "").strip():
        raise DomainError("Amount and a reason are required.")
    outcome = None
    if kind == WageAdjustment.DEDUCTION:
        if not rejection_outcome_id:
            raise DomainError("A deduction needs a rejection reference.")
        outcome = StageOutcome.objects.select_related("work_entry").filter(pk=rejection_outcome_id, business=business).first()
        if outcome is None or outcome.work_entry is None or outcome.work_entry.worker_id != st.worker_id:
            raise DomainError("The rejection reference must identify this worker (a work-entry outcome).")
        if outcome.rejected_worker_qty <= 0:
            raise DomainError("The referenced outcome has no worker-attributable rejection.")
    elif kind != WageAdjustment.EARNING:
        raise DomainError("Invalid adjustment kind.")
    adj = WageAdjustment.objects.create(business=business, statement=st, kind=kind, amount=amount,
                                        reason=reason.strip(), rejection_outcome=outcome, proposed_by=user)
    record_audit(business=business, user=user, action="wage_adjustment.propose", entity="WageAdjustment",
                 entity_id=adj.pk, after={"kind": kind, "amount": amount, "reason": reason})
    return adj


@transaction.atomic
def decide_adjustment(*, business, user, adjustment_id, approve, note=""):
    from accounts.permissions import OWNER
    if user.effective_role != OWNER:
        raise DomainError("Only the Owner can approve or reject wage adjustments.")
    adj = WageAdjustment.objects.select_for_update().select_related("statement").get(pk=adjustment_id, business=business)
    if adj.status != WageAdjustment.PROPOSED:
        raise DomainError("This adjustment was already decided.")
    if adj.statement.status != WageStatement.DRAFT:
        raise DomainError("The statement is finalized.")
    self_approval = adj.proposed_by_id == user.pk
    WageAdjustmentDecision.objects.create(adjustment=adj, approved=approve, decided_by=user,
                                          is_self_approval=self_approval, note=note)
    WageAdjustment.objects.filter(pk=adj.pk).update(
        status=WageAdjustment.APPROVED if approve else WageAdjustment.REJECTED)
    record_audit(business=business, user=user, action="wage_adjustment.decide", entity="WageAdjustment",
                 entity_id=adj.pk, after={"approved": approve, "self_approval": self_approval},
                 flagged=self_approval)
    return adj


@transaction.atomic
def finalize_statement(*, business, user, statement_id, advance_recovery=0):
    st = WageStatement.objects.select_for_update().get(pk=statement_id, business=business)
    if st.status != WageStatement.DRAFT:
        raise DomainError("Statement is already finalized.")
    if st.adjustments.filter(status=WageAdjustment.PROPOSED).exists():
        raise DomainError("Resolve (approve/reject) all proposed adjustments before finalizing.")
    gross = st.lines.aggregate(t=Sum("amount"))["t"] or ZERO
    add = st.adjustments.filter(status="approved", kind="earning").aggregate(t=Sum("amount"))["t"] or ZERO
    ded = st.adjustments.filter(status="approved", kind="deduction").aggregate(t=Sum("amount"))["t"] or ZERO
    before = gross + add - ded
    if before < 0:
        raise DomainError("Deductions exceed earnings.")
    recovery = D(advance_recovery)
    if recovery < 0 or recovery > before:
        raise DomainError("Advance recovery cannot exceed the amount payable.")
    if recovery > advance_balance(st.worker):
        raise DomainError(f"Advance recovery exceeds the worker's advance balance ({advance_balance(st.worker)}).")
    number = next_number(business, "wage_statement")
    if recovery > 0:
        WorkerAdvance.objects.create(business=business, worker=st.worker, direction="recovered",
                                     source="statement_recovery", amount=recovery,
                                     business_date=timezone.localdate(), statement=st, created_by=user)
    WageStatement.objects.filter(pk=st.pk).update(
        status=WageStatement.FINALIZED, number=number, gross_earnings=gross, additional_earnings=add,
        deductions=ded, advance_recovery=recovery, net_payable=before - recovery,
        finalized_at=timezone.now(), finalized_by=user)
    record_audit(business=business, user=user, action="wage_statement.finalize", entity="WageStatement",
                 entity_id=st.pk, after={"number": number, "net": before - recovery})
    st.refresh_from_db()
    return st


@transaction.atomic
def pay_statement(*, business, user, statement_id, amount, account_id, payment_date=None):
    """Pay a finalized statement. Anything above the outstanding wages becomes a worker advance."""
    st = WageStatement.objects.select_for_update().select_related("worker").get(pk=statement_id, business=business)
    if st.status != WageStatement.FINALIZED:
        raise DomainError("Payment is allowed only after finalization.")
    amount = D(amount)
    if amount <= 0:
        raise DomainError("Payment amount must be positive.")
    payment_date = payment_date or timezone.localdate()
    if payment_date > timezone.localdate():
        raise DomainError("Payment date cannot be in the future.")
    acc = get_account(business, account_id)
    applied = min(amount, st.net_payable - st.paid_amount)
    excess = amount - applied
    pay = WagePayment.objects.create(business=business, statement=st, account=acc, amount=amount,
                                     applied_amount=applied, excess_amount=excess, payment_date=payment_date,
                                     created_by=user)
    if excess > 0:
        WorkerAdvance.objects.create(business=business, worker=st.worker, direction="issued",
                                     source="excess_payment", amount=excess, business_date=payment_date,
                                     statement=st, created_by=user)
    WageStatement.objects.filter(pk=st.pk).update(paid_amount=st.paid_amount + applied)
    add_cashbook(business=business, account=acc, entry_date=payment_date, direction="out", amount=amount,
                 narration=f"Wages {st.number} - {st.worker.name}", wage_payment=pay)   # exactly one entry per payment
    record_audit(business=business, user=user, action="wage_payment.create", entity="WagePayment", entity_id=pay.pk,
                 after={"statement": st.number, "amount": amount, "applied": applied, "excess": excess})
    return pay


@transaction.atomic
def issue_advance(*, business, user, worker_id, amount, account_id, business_date=None):
    worker = Worker.objects.get(pk=worker_id, business=business)
    amount = D(amount)
    if amount <= 0:
        raise DomainError("Advance amount must be positive.")
    acc = get_account(business, account_id)
    business_date = business_date or timezone.localdate()
    adv = WorkerAdvance.objects.create(business=business, worker=worker, direction="issued", source="cash_advance",
                                       amount=amount, business_date=business_date, created_by=user)
    add_cashbook(business=business, account=acc, entry_date=business_date, direction="out", amount=amount,
                 narration=f"Advance - {worker.name}", worker_advance=adv)
    record_audit(business=business, user=user, action="worker_advance.issue", entity="WorkerAdvance", entity_id=adv.pk,
                 after={"worker": worker.code, "amount": amount})
    return adv
