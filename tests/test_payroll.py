from datetime import timedelta
from decimal import Decimal as D

from django.db import IntegrityError, transaction

from core.exceptions import DomainError, ImmutableRecordError
from finance.models import CashBookEntry
from payroll import services as pay
from payroll.models import WageAdjustment, WageAdjustmentDecision, WagePayment, WageStatement, WorkerAdvance
from production import services as prod
from production.models import StageOutcome, WorkEntry
from .base import ERPTestCase


class Payroll(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.batch = self.make_batch(q28=50)
        self.stock_fabric()
        self.cut = self.entry(self.batch, self.op_cut, "CUT", good=50)                                       # 400
        self.stitch = self.entry(self.batch, self.op_stitch, "STITCH", worker=self.worker2, good=40, rejected_worker=5, rework_other=5, reason="stitching_defect")  # paid 45 x 40 = 1800
        self.start, self.end = self.today - timedelta(days=6), self.today

    def statement(self, worker=None):
        return pay.create_statement(business=self.b, user=self.acct, worker_id=(worker or self.worker).pk, period_start=self.start, period_end=self.end)

    def test_statement_aggregates_unattached_entries(self):
        st = self.statement(self.worker2)
        self.assertEqual(st.gross_earnings, D("1800.00"))
        self.assertEqual(st.lines.count(), 1)

    def test_entry_belongs_to_one_statement_and_locks(self):
        self.statement()
        with self.assertRaises(DomainError):
            prod.delete_work_entry(business=self.b, user=self.prodn, entry_id=self.cut.pk)
        with self.assertRaises(DomainError):          # nothing left to attach
            pay.create_statement(business=self.b, user=self.acct, worker_id=self.worker.pk, period_start=self.start - timedelta(days=30), period_end=self.start - timedelta(days=24))

    def test_overlapping_periods_rejected_by_database(self):
        self.statement()
        with self.assertRaises(DomainError):
            pay.create_statement(business=self.b, user=self.acct, worker_id=self.worker.pk, period_start=self.today - timedelta(days=2), period_end=self.today + timedelta(days=3))
        # raw DB-level proof of the exclusion constraint
        with self.assertRaises(IntegrityError), transaction.atomic():
            WageStatement.objects.create(business=self.b, worker=self.worker, period_start=self.today, period_end=self.today, created_by=self.acct)

    def test_unattached_entry_can_be_deleted(self):
        prod.delete_work_entry(business=self.b, user=self.prodn, entry_id=self.stitch.pk)
        self.assertFalse(WorkEntry.objects.filter(pk=self.stitch.pk).exists())
        self.entry(self.batch, self.op_stitch, "STITCH", worker=self.worker2, good=1)
        with self.assertRaises(DomainError):       # later stages now depend on the cutting output
            prod.delete_work_entry(business=self.b, user=self.prodn, entry_id=self.cut.pk)

    def test_finalize_makes_statement_immutable_and_blocks_delete(self):
        st = pay.finalize_statement(business=self.b, user=self.acct, statement_id=self.statement().pk)
        self.assertEqual((st.status, st.net_payable), ("finalized", D("400.00")))
        self.assertTrue(st.number.startswith("WS/"))
        st.gross_earnings = D("1")
        with self.assertRaises(ImmutableRecordError):
            st.save()
        with self.assertRaises(DomainError):
            pay.delete_draft_statement(business=self.b, user=self.acct, statement_id=st.pk)
        with self.assertRaises(DomainError):
            pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)

    def test_payment_only_after_finalization_and_one_cashbook_entry_each(self):
        st = self.statement()
        with self.assertRaises(DomainError):
            pay.pay_statement(business=self.b, user=self.acct, statement_id=st.pk, amount=D("100"), account_id=self.cash.pk)
        pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)
        p1 = pay.pay_statement(business=self.b, user=self.acct, statement_id=st.pk, amount=D("150"), account_id=self.cash.pk)
        p2 = pay.pay_statement(business=self.b, user=self.acct, statement_id=st.pk, amount=D("250"), account_id=self.bank.pk)
        st.refresh_from_db()
        self.assertEqual((st.paid_amount, st.outstanding), (D("400.00"), D("0.00")))
        self.assertEqual(CashBookEntry.objects.filter(wage_payment__in=[p1, p2]).count(), 2)
        self.assertEqual(p1.cashbook_entry.direction, "out")

    def test_excess_payment_becomes_worker_advance(self):
        st = self.statement()
        pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)
        p = pay.pay_statement(business=self.b, user=self.acct, statement_id=st.pk, amount=D("500"), account_id=self.cash.pk)
        self.assertEqual((p.applied_amount, p.excess_amount), (D("400.00"), D("100.00")))
        self.assertEqual(pay.advance_balance(self.worker), D("100.00"))
        self.assertEqual(CashBookEntry.objects.get(wage_payment=p).amount, D("500.00"))
        with self.assertRaises(ImmutableRecordError):
            p.amount = D("1")
            p.save()

    def test_advance_recovery_limits_and_balance(self):
        pay.issue_advance(business=self.b, user=self.acct, worker_id=self.worker.pk, amount=D("300"), account_id=self.cash.pk)
        self.assertEqual(CashBookEntry.objects.filter(worker_advance__isnull=False).count(), 1)
        st = self.statement()
        with self.assertRaises(DomainError):
            pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk, advance_recovery=D("301"))
        done = pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk, advance_recovery=D("120"))
        self.assertEqual((done.advance_recovery, done.net_payable), (D("120.00"), D("280.00")))
        self.assertEqual(pay.advance_balance(self.worker), D("180.00"))

    def test_adjustments_require_owner_approval_and_flag_self_approval(self):
        st = self.statement(self.worker2)
        out = StageOutcome.objects.get(work_entry=self.stitch)
        with self.assertRaises(DomainError):          # deduction without rejection reference
            pay.propose_adjustment(business=self.b, user=self.acct, statement_id=st.pk, kind="deduction", amount=D(10), reason="r")
        with self.assertRaises(DomainError):          # reference must identify this worker
            other = StageOutcome.objects.filter(work_entry__worker=self.worker).first()
            pay.propose_adjustment(business=self.b, user=self.acct, statement_id=st.pk, kind="deduction", amount=D(10), reason="r", rejection_outcome_id=other.pk)
        with self.assertRaises(DomainError):          # reason mandatory
            pay.propose_adjustment(business=self.b, user=self.acct, statement_id=st.pk, kind="earning", amount=D(10), reason=" ")
        a = pay.propose_adjustment(business=self.b, user=self.acct, statement_id=st.pk, kind="deduction", amount=D(100), reason="defects", rejection_outcome_id=out.pk)
        with self.assertRaises(DomainError):          # accounts cannot approve
            pay.decide_adjustment(business=self.b, user=self.acct, adjustment_id=a.pk, approve=True)
        with self.assertRaises(DomainError):          # pending proposals block finalization
            pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)
        pay.decide_adjustment(business=self.b, user=self.owner, adjustment_id=a.pk, approve=True)
        self.assertFalse(a.decision.is_self_approval)
        done = pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)
        self.assertEqual((done.deductions, done.net_payable), (D("100.00"), D("1700.00")))
        dec = WageAdjustmentDecision.objects.get(adjustment=a)
        dec.approved = False
        with self.assertRaises(ImmutableRecordError):
            dec.save()

    def test_owner_self_approval_is_flagged_in_audit(self):
        from core.models import AuditEvent
        st = self.statement()
        a = pay.propose_adjustment(business=self.b, user=self.owner, statement_id=st.pk, kind="earning", amount=D(50), reason="bonus")
        pay.decide_adjustment(business=self.b, user=self.owner, adjustment_id=a.pk, approve=True)
        self.assertTrue(a.decision.is_self_approval)
        self.assertTrue(AuditEvent.objects.filter(action="wage_adjustment.decide", flagged=True).exists())

    def test_unidentified_inspection_rejection_never_reduces_wages(self):
        prod.record_inspection(business=self.b, user=self.prodn, batch_stage_id=self.stages["QC"].pk, variant_id=self.v28.pk, inspected_date=self.today,
                               good=2, rejected_worker=3, reason="stitching_defect")
        st = self.statement(self.worker2)
        done = pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)
        self.assertEqual((done.deductions, done.net_payable), (D("0.00"), D("1800.00")))

    def test_payroll_reconciliation(self):
        from reports import reconciliation as rc
        st = self.statement()
        pay.finalize_statement(business=self.b, user=self.acct, statement_id=st.pk)
        pay.pay_statement(business=self.b, user=self.acct, statement_id=st.pk, amount=D("100"), account_id=self.cash.pk)
        self.assertTrue(rc.payroll(self.b).ok and rc.wage_payments(self.b).ok and rc.cashbook(self.b).ok)
