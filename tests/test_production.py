from datetime import timedelta
from decimal import Decimal as D

from django.db import IntegrityError, transaction

from core.exceptions import DomainError, ImmutableRecordError, InsufficientStock
from inventory.models import StockBalance
from production import services as prod
from production.models import (Batch, FinishedGoodsReceipt, OverCutVariance, RateCard, ReworkLink, StageOutcome, WorkEntry)
from .base import ERPTestCase


class Workflow(ERPTestCase):
    def test_stage_at_most_once(self):
        with self.assertRaises(DomainError):
            prod.create_workflow(business=self.b, user=self.owner, name="Dup", steps=[(self.st["CUT"], "work_entry"), (self.st["CUT"], "work_entry")])

    def test_batch_snapshots_workflow_and_fingerprint_ignores_names(self):
        batch = self.make_batch()
        self.assertEqual(batch.stages.count(), 4)
        fp = batch.workflow_fingerprint
        self.assertEqual(len(fp), 64)
        self.st["CUT"].name = "Cutting Renamed"
        self.st["CUT"].save()
        rows = [(s.stage_id, s.sequence, s.outcome_source) for s in batch.stages.all()]
        self.assertEqual(prod.stage_fingerprint(rows), fp)
        # editing the workflow later does not change the existing batch
        self.wf.steps.filter(stage=self.st["FG"]).delete()
        self.assertEqual(Batch.objects.get(pk=batch.pk).stages.count(), 4)

    def test_batch_requires_default_workflow(self):
        from production.models import ProductWorkflow
        ProductWorkflow.objects.all().delete()
        with self.assertRaises(DomainError):
            self.make_batch()


class Outcomes(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.batch = self.make_batch(q28=20)
        self.stock_fabric(qty="100", cost="10")

    def test_processed_must_equal_parts_db_constraint(self):
        e = self.entry(self.batch, self.op_cut, "CUT", good=20)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StageOutcome.objects.filter(work_entry=e).update(good_qty=D("19"))

    def test_counting_entry_creates_outcome_and_payroll_only_does_not(self):
        e = self.entry(self.batch, self.op_cut, "CUT", good=10)
        self.assertEqual(e.outcome.processed_qty, D("10"))
        p = self.entry(self.batch, self.op_pay, "STITCH", quantity=D("7"))
        self.assertFalse(StageOutcome.objects.filter(work_entry=p).exists())
        self.assertEqual(p.paid_qty, D("7"))
        with self.assertRaises(DomainError):    # payroll-only takes no outcome breakdown
            self.entry(self.batch, self.op_pay, "STITCH", good=3)

    def test_manual_entry_affects_payroll_only(self):
        before = StageOutcome.objects.count(), StockBalance.objects.count()
        e = prod.record_work_entry(business=self.b, user=self.prodn, worker_id=self.worker.pk, operation_id=self.op_pay.pk, work_date=self.today, quantity=D("50"))
        self.assertTrue(e.is_manual and e.batch_stage_id is None)
        self.assertEqual((StageOutcome.objects.count(), StockBalance.objects.count()), before)
        with self.assertRaises(DomainError):
            prod.record_work_entry(business=self.b, user=self.prodn, worker_id=self.worker.pk, operation_id=self.op_rework.pk, work_date=self.today, quantity=D("5"))

    def test_next_stage_limited_by_previous_good(self):
        self.entry(self.batch, self.op_cut, "CUT", good=20)
        self.entry(self.batch, self.op_stitch, "STITCH", good=15)
        with self.assertRaises(DomainError):
            self.entry(self.batch, self.op_stitch, "STITCH", good=6)      # only 5 left

    def test_operation_must_match_stage_and_source(self):
        with self.assertRaises(DomainError):
            self.entry(self.batch, self.op_stitch, "CUT", good=1)
        with self.assertRaises(DomainError):           # QC records by inspection, not operation entries
            self.entry(self.batch, self.op_stitch, "QC", good=1)

    def test_rejection_reason_required_and_other_needs_text(self):
        self.entry(self.batch, self.op_cut, "CUT", good=20)
        with self.assertRaises(DomainError):
            self.entry(self.batch, self.op_stitch, "STITCH", good=8, rejected_worker=2)
        with self.assertRaises(DomainError):
            self.entry(self.batch, self.op_stitch, "STITCH", good=8, rejected_worker=2, reason="other")
        e = self.entry(self.batch, self.op_stitch, "STITCH", good=8, rejected_worker=2, reason="other", note="torn collar")
        self.assertEqual(e.rejection_reason, "other")

    def test_deactivated_operation_blocks_new_entries(self):
        prod.set_operation_active(business=self.b, user=self.prodn, operation_id=self.op_cut.pk, active=False)
        with self.assertRaises(DomainError):
            self.entry(self.batch, self.op_cut, "CUT", good=1)


class PayRules(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.batch = self.make_batch(q28=40)
        self.stock_fabric()
        self.entry(self.batch, self.op_cut, "CUT", good=40)

    def test_original_operation_pays_good_plus_other_not_worker_attributable(self):
        e = self.entry(self.batch, self.op_stitch, "STITCH", good=20, rejected_worker=3, rejected_other=2, rework_worker=4, rework_other=5, reason="fabric_defect")
        self.assertEqual(e.processed_qty, D("34"))
        self.assertEqual(e.paid_qty, D("27"))          # good 20 + rejected_other 2 + rework_other 5
        self.assertEqual(e.earnings, D("1080.00"))     # 27 x 40

    def test_rework_pays_good_plus_rejected_other(self):
        s = self.entry(self.batch, self.op_stitch, "STITCH", good=20, rework_worker=3, rework_other=3, reason="stitching_defect")
        r = self.entry(self.batch, self.op_rework, "STITCH", good=4, rejected_other=1, rejected_worker=1, reason="stain_damage", rework_links=[(s.pk, 6)])
        self.assertEqual(r.paid_qty, D("5"))
        self.assertEqual(r.earnings, D("100.00"))

    def test_rework_links_rules(self):
        s = self.entry(self.batch, self.op_stitch, "STITCH", good=20, rework_worker=4, reason="stitching_defect")
        with self.assertRaises(DomainError):            # no link
            self.entry(self.batch, self.op_rework, "STITCH", good=4)
        with self.assertRaises(DomainError):            # link total != completed
            self.entry(self.batch, self.op_rework, "STITCH", good=4, rework_links=[(s.pk, 3)])
        with self.assertRaises(DomainError):            # exceeds sent to rework
            self.entry(self.batch, self.op_rework, "STITCH", good=5, rework_links=[(s.pk, 5)])
        self.entry(self.batch, self.op_rework, "STITCH", good=3, rework_links=[(s.pk, 3)])
        with self.assertRaises(DomainError):            # only 1 left
            self.entry(self.batch, self.op_rework, "STITCH", good=2, rework_links=[(s.pk, 2)])
        self.assertEqual(ReworkLink.objects.filter(source_entry=s).count(), 1)

    def test_rework_good_counts_toward_stage_output(self):
        s = self.entry(self.batch, self.op_stitch, "STITCH", good=30, rework_worker=10, reason="stitching_defect")
        self.entry(self.batch, self.op_rework, "STITCH", good=10, rework_links=[(s.pk, 10)])
        self.assertEqual(prod.stage_totals(self.stages["STITCH"], self.v28)["good"], D("40"))


class Inspection(ERPTestCase):
    def test_inspection_and_finished_goods_limits(self):
        batch = self.make_batch(q28=10)
        self.stock_fabric()
        for stage, op in (("CUT", self.op_cut), ("STITCH", self.op_stitch)):
            self.entry(batch, op, stage, good=10)
        with self.assertRaises(DomainError):     # more than stitched
            prod.record_inspection(business=self.b, user=self.prodn, batch_stage_id=self.stages["QC"].pk, variant_id=self.v28.pk, inspected_date=self.today, good=11)
        prod.record_inspection(business=self.b, user=self.prodn, batch_stage_id=self.stages["QC"].pk, variant_id=self.v28.pk, inspected_date=self.today,
                               good=8, rejected_worker=1, rejected_other=1, reason="size_error")
        self.assertEqual(StageOutcome.objects.filter(source="inspection").count(), 1)
        with self.assertRaises(DomainError):
            prod.receive_finished_goods(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.v28.pk, quantity=9)
        prod.receive_finished_goods(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.v28.pk, quantity=5)
        prod.receive_finished_goods(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.v28.pk, quantity=3)
        with self.assertRaises(DomainError):
            prod.receive_finished_goods(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.v28.pk, quantity=1)
        self.assertEqual(StockBalance.objects.get(variant=self.v28).quantity, D("8"))
        prod.complete_batch(business=self.b, user=self.prodn, batch_id=batch.pk)

    def test_direct_inspection_cannot_send_to_rework_db_constraint(self):
        batch = self.make_batch()
        self.stock_fabric()
        for stage, op in (("CUT", self.op_cut), ("STITCH", self.op_stitch)):
            self.entry(batch, op, stage, good=10)
        o = prod.record_inspection(business=self.b, user=self.prodn, batch_stage_id=self.stages["QC"].pk, variant_id=self.v28.pk, inspected_date=self.today, good=10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StageOutcome.objects.filter(pk=o.pk).update(rework_other_qty=D("1"), good_qty=D("9"))

    def test_inspection_cannot_hit_a_work_entry_stage(self):
        batch = self.make_batch()
        with self.assertRaises(DomainError):
            prod.record_inspection(business=self.b, user=self.prodn, batch_stage_id=self.stages["CUT"].pk, variant_id=self.v28.pk, inspected_date=self.today, good=1)


class OverCutting(ERPTestCase):
    def test_overcut_needs_reason_and_material_and_records_variance_with_issue(self):
        batch = self.make_batch(q28=10)
        self.stock_fabric(qty="50", cost="10")
        with self.assertRaises(DomainError):
            self.entry(batch, self.op_cut, "CUT", good=12)
        with self.assertRaises(DomainError):
            self.entry(batch, self.op_cut, "CUT", good=12, over_cut_reason="flaws")
        e = self.entry(batch, self.op_cut, "CUT", good=12, over_cut_reason="flaws", extra_materials=[(self.fabric.pk, D("2"))])
        var = OverCutVariance.objects.get()
        self.assertEqual((var.extra_qty, var.variant_id, var.batch_stage_id), (D("2"), self.v28.pk, self.stages["CUT"].pk))
        self.assertEqual(var.material_issues.get().quantity, D("2"))
        self.assertEqual(StockBalance.objects.get(variant=self.fabric).quantity, D("48"))

    def test_overcut_with_insufficient_stock_rolls_back_everything(self):
        batch = self.make_batch(q28=10)
        self.stock_fabric(qty="1", cost="10")
        with self.assertRaises(InsufficientStock):
            self.entry(batch, self.op_cut, "CUT", good=12, over_cut_reason="flaws", extra_materials=[(self.fabric.pk, D("5"))])
        self.assertEqual((WorkEntry.objects.count(), OverCutVariance.objects.count(), StageOutcome.objects.count()), (0, 0, 0))
        self.assertEqual(StockBalance.objects.get(variant=self.fabric).quantity, D("1"))


class Material(ERPTestCase):
    def test_issue_uses_weighted_average_and_blocks_negative(self):
        batch = self.make_batch()
        self.stock_fabric(qty="10", cost="10")
        self.stock_fabric(qty="10", cost="20")
        i = prod.issue_material(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.fabric.pk, quantity=D("10"))
        self.assertEqual(i.value, D("150.00"))
        with self.assertRaises(InsufficientStock):
            prod.issue_material(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.fabric.pk, quantity=D("11"))
        with self.assertRaises(DomainError):
            prod.issue_material(business=self.b, user=self.prodn, batch_id=batch.pk, variant_id=self.v28.pk, quantity=D("1"))


class Rates(ERPTestCase):
    def card(self, rate, days_ago=0, **kw):
        return prod.create_rate_card(business=self.b, user=self.owner, operation=self.op_cut, rate=D(rate),
                                     effective_from=self.today - timedelta(days=days_ago), reason="t", **kw)

    def pick(self, worker=None, product=None, variant=None, date=None):
        return prod.select_rate(business=self.b, operation=self.op_cut, worker=worker, product=product, variant=variant, work_date=date or self.today)

    def test_precedence_order(self):
        self.assertEqual(self.pick(self.worker, self.shirt, self.v28)[0], "default")
        self.card(9, product=self.shirt)
        self.assertEqual(self.pick(self.worker, self.shirt, self.v28)[0], "product")
        self.card(10, product=self.shirt, variant=self.v28)
        self.assertEqual(self.pick(self.worker, self.shirt, self.v28)[0], "product+variant")
        self.card(11, worker=self.worker)
        self.assertEqual(self.pick(self.worker, self.shirt, self.v28)[0], "worker")
        self.card(12, worker=self.worker, product=self.shirt)
        self.assertEqual(self.pick(self.worker, self.shirt, self.v28)[0], "worker+product")
        self.card(13, worker=self.worker, product=self.shirt, variant=self.v28)
        tier, c = self.pick(self.worker, self.shirt, self.v28)
        self.assertEqual((tier, c.rate), ("worker+product+variant", D("13.00")))
        self.assertEqual(self.pick(self.worker2, self.shirt, self.v28)[0], "product+variant")

    def test_latest_effective_date_wins_within_tier(self):
        self.card(20, days_ago=10, worker=self.worker)
        self.card(25, days_ago=3, worker=self.worker)
        self.assertEqual(self.pick(self.worker)[1].rate, D("25.00"))
        self.assertEqual(self.pick(self.worker, date=self.today - timedelta(days=5))[1].rate, D("20.00"))

    def test_rate_cards_immutable(self):
        c = self.card(30)
        c.rate = D("1")
        with self.assertRaises(ImmutableRecordError):
            c.save()

    def test_backdated_cards_owner_only_with_reason(self):
        with self.assertRaises(DomainError):
            prod.create_rate_card(business=self.b, user=self.acct, operation=self.op_cut, rate=D(5), effective_from=self.today - timedelta(days=1), reason="x")
        with self.assertRaises(DomainError):
            prod.create_rate_card(business=self.b, user=self.owner, operation=self.op_cut, rate=D(5), effective_from=self.today - timedelta(days=1), reason=" ")
        c = prod.create_rate_card(business=self.b, user=self.acct, operation=self.op_cut, rate=D(5), effective_from=self.today)
        self.assertFalse(c.is_backdated)

    def test_changing_rates_never_changes_previous_entries(self):
        batch = self.make_batch(q28=10)
        e = self.entry(batch, self.op_cut, "CUT", good=10)
        self.assertEqual((e.rate, e.earnings, e.rate_tier), (D("8.00"), D("80.00"), "default"))
        self.card(99, worker=self.worker)
        e.refresh_from_db()
        self.assertEqual((e.rate, e.earnings), (D("8.00"), D("80.00")))

    def test_no_rate_card_is_an_error(self):
        op = prod.create_operation(business=self.b, user=self.prodn, name="Unrated", is_payroll_only=True)
        with self.assertRaises(DomainError):
            prod.record_work_entry(business=self.b, user=self.prodn, worker_id=self.worker.pk, operation_id=op.pk, work_date=self.today, quantity=D(1))


class SnapshotChecks(ERPTestCase):
    def test_reconciliation_passes_for_clean_production_and_flags_tampering(self):
        from reports import reconciliation as rc
        batch = self.make_batch(q28=10)
        self.stock_fabric()
        self.entry(batch, self.op_cut, "CUT", good=10)
        self.assertTrue(rc.production_quantities(self.b).ok and rc.rework(self.b).ok and rc.snapshots(self.b).ok)
        from production.models import BatchStage
        BatchStage.objects.filter(batch=batch, sequence=2).update(outcome_source="inspection")   # tamper (raw SQL-level update)
        res = rc.snapshots(self.b)
        self.assertFalse(res.ok)       # reported, not repaired
        self.assertEqual(BatchStage.objects.get(batch=batch, sequence=2).outcome_source, "inspection")
