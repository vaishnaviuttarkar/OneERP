from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from catalogue import services as cat
from core.models import Business, BusinessSettings, FinancialAccount
from parties import services as par
from production import services as prod
from production.models import StageDefinition


class ERPTestCase(TestCase):
    """Runs on PostgreSQL (see settings). Builds a small business used by every test module."""

    def setUp(self):
        self.today = timezone.localdate()
        self.b = Business.objects.create(name="Test Uniforms", slug="test")
        BusinessSettings.objects.create(business=self.b)
        self.cash = FinancialAccount.objects.create(business=self.b, name="Cash", kind="cash")
        self.bank = FinancialAccount.objects.create(business=self.b, name="Bank", kind="bank")
        self.owner = User.objects.create_user("owner", password="x", role=User.OWNER)
        self.acct = User.objects.create_user("acct", password="x", role=User.ACCOUNTS)
        self.prodn = User.objects.create_user("prod", password="x", role=User.PRODUCTION)
        self.st = {c: StageDefinition.objects.create(business=self.b, name=n, code=c, default_outcome_source=s) for c, n, s in [
            ("CUT", "Cutting", "work_entry"), ("STITCH", "Stitching", "work_entry"), ("QC", "Inspection", "inspection"), ("FG", "Finished", "none")]}
        self.wf = prod.create_workflow(business=self.b, user=self.owner, name="WF",
                                       steps=[(self.st[c], self.st[c].default_outcome_source) for c in ("CUT", "STITCH", "QC", "FG")])
        self.shirt = cat.create_product(business=self.b, user=self.acct, name="Shirt", kind="finished", default_workflow=self.wf)
        self.v28 = cat.add_variant(business=self.b, user=self.acct, product=self.shirt, sku="SH28", size="28", colour="White")
        self.v30 = cat.add_variant(business=self.b, user=self.acct, product=self.shirt, sku="SH30", size="30", colour="White")
        fab = cat.create_product(business=self.b, user=self.acct, name="Fabric", kind="raw")
        self.fabric = cat.add_variant(business=self.b, user=self.acct, product=fab, sku="FAB", unit="m")
        self.cust = par.create_party(business=self.b, user=self.acct, name="School A", is_customer=True)
        self.sup = par.create_party(business=self.b, user=self.acct, name="Supplier A", is_supplier=True)
        self.worker = prod.create_worker(business=self.b, user=self.prodn, name="Asha", code="W1")
        self.worker2 = prod.create_worker(business=self.b, user=self.prodn, name="Bela", code="W2")
        self.op_cut = prod.create_operation(business=self.b, user=self.prodn, name="Cutting", stage=self.st["CUT"])
        self.op_stitch = prod.create_operation(business=self.b, user=self.prodn, name="Stitching", stage=self.st["STITCH"])
        self.op_rework = prod.create_operation(business=self.b, user=self.prodn, name="Rework", stage=self.st["STITCH"], is_rework=True)
        self.op_pay = prod.create_operation(business=self.b, user=self.prodn, name="Buttons", is_payroll_only=True)
        for op, rate in ((self.op_cut, 8), (self.op_stitch, 40), (self.op_rework, 20), (self.op_pay, 4)):
            prod.create_rate_card(business=self.b, user=self.owner, operation=op, rate=D(rate),
                                  effective_from=self.today - timedelta(days=60), reason="setup")

    # helpers
    def stock_fabric(self, qty="100", cost="10", tax="0"):
        from purchasing import services as pur
        n = getattr(self, "_bn", 0) + 1
        self._bn = n
        bill = pur.create_bill(business=self.b, user=self.acct, supplier_id=self.sup.pk, supplier_bill_number=f"B{n}", bill_date=self.today,
                               lines=[{"variant_id": self.fabric.pk, "quantity": D(qty), "unit_cost": D(cost), "tax_amount": D(tax)}])
        return pur.post_bill(business=self.b, user=self.acct, bill_id=bill.pk)

    def make_batch(self, q28=10, q30=0):
        plans = [(self.v28, q28)] + ([(self.v30, q30)] if q30 else [])
        batch = prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, plans=plans, start_date=self.today)
        self.stages = {s.stage.code: s for s in batch.stages.select_related("stage")}
        return batch

    def entry(self, batch, op, stage, variant=None, worker=None, **kw):
        return prod.record_work_entry(business=self.b, user=self.prodn, batch_id=batch.pk, worker_id=(worker or self.worker).pk, operation_id=op.pk,
                                      work_date=self.today, variant_id=(variant or self.v28).pk, batch_stage_id=self.stages[stage].pk, **kw)

    def stock_fg(self, variant, qty):
        from inventory.models import StockMovement
        from inventory.services import post_movement
        return post_movement(business=self.b, variant=variant, movement_type=StockMovement.OPENING, quantity=D(qty), user=self.acct)
