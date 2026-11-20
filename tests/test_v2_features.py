from decimal import Decimal as D

from django.test import Client

from core.exceptions import DomainError
from core.utils import PAID, PARTIAL, UNPAID, payment_status
from parties import services as par
from production import services as prod
from production.models import Batch
from .base import ERPTestCase


class BatchWorkflowAndClient(ERPTestCase):
    """Client per batch, fabric/GSM per size, and the per-batch workflow checklist."""

    def test_batch_saves_client_fabric_and_gsm(self):
        batch = prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today, client=self.cust,
                                  plans=[(self.v28, 10, "Poly-cotton", 180)])
        self.assertEqual(batch.client_id, self.cust.pk)
        plan = batch.plans.get()
        self.assertEqual((plan.fabric_type, plan.gsm), ("Poly-cotton", 180))

    def test_client_must_be_an_active_customer(self):
        with self.assertRaises(DomainError):
            prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today, client=self.sup,
                              plans=[(self.v28, 10)])

    def test_gsm_range_is_validated(self):
        with self.assertRaises(DomainError):
            prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today, plans=[(self.v28, 10, "", 5000)])

    def test_ticked_stages_become_the_batch_route_in_configured_order(self):
        batch = prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today,
                                  stages=[self.st["FG"], self.st["STITCH"], self.st["CUT"]], plans=[(self.v28, 10)])
        self.assertEqual([s.stage.code for s in batch.stages.order_by("sequence")], ["CUT", "STITCH", "FG"])
        self.assertEqual(batch.stages.count(), 3)

    def test_no_stage_ticked_is_rejected(self):
        with self.assertRaises(DomainError):
            prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today, stages=[], plans=[(self.v28, 10)])

    def test_route_cannot_start_with_direct_inspection(self):
        with self.assertRaises(DomainError):
            prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today,
                              stages=[self.st["QC"]], plans=[(self.v28, 10)])

    def test_default_workflow_used_when_no_checklist_given(self):
        self.assertEqual(self.make_batch().stages.count(), 4)

    def test_client_and_dates_editable_but_quantities_lock_after_production(self):
        batch = self.make_batch(q28=20)
        prod.update_batch(business=self.b, user=self.owner, batch_id=batch.pk, start_date=self.today, client=self.cust,
                          plans=[(self.v28, 25, "Cotton", 200)])
        batch.refresh_from_db()
        self.assertEqual(batch.client_id, self.cust.pk)
        self.assertEqual(batch.planned_qty, 25)
        self.entry(batch, self.op_cut, "CUT", good=5)
        with self.assertRaises(DomainError):
            prod.update_batch(business=self.b, user=self.owner, batch_id=batch.pk, start_date=self.today, client=self.cust,
                              plans=[(self.v28, 30, "Cotton", 200)])

    def test_batch_with_activity_cannot_be_deleted(self):


class ResetAndPages(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.c = Client()
        self.c.login(username="owner", password="x")

    def test_reset_only_when_filtered(self):
        self.assertNotIn('class="btn btn-reset"', self.c.get("/parties/").content.decode())
        self.assertIn('class="btn btn-reset"', self.c.get("/parties/", {"role": "customer"}).content.decode())

    def test_no_apply_button_and_compact_filter_chips(self):
        h = self.c.get("/parties/").content.decode()
        self.assertNotIn("<span>Apply</span>", h)
        self.assertIn("filter-chip", h)
        self.assertIn('hx-select-oob="#reset-slot"', h)

    def test_row_actions_present_on_parties_and_products(self):
        h = self.c.get("/parties/").content.decode()
        self.assertIn(f"/parties/{self.cust.pk}/edit/", h)
        self.assertIn(f"/parties/{self.cust.pk}/delete/", h)
        self.assertIn(f"/catalogue/products/{self.shirt.pk}/edit/", self.c.get("/catalogue/products/").content.decode())

    def test_new_pages_render(self):
        batch = self.make_batch()
        urls = ["/", "/finance/", "/production/batches/", "/production/batches/new/", f"/production/batches/{batch.pk}/",
                f"/production/batches/{batch.pk}/edit/", "/production/stages/", "/production/workflows/", "/payroll/",
                "/purchasing/", "/parties/", f"/parties/{self.cust.pk}/edit/", "/catalogue/products/"]
        for u in urls:
            self.assertEqual(self.c.get(u).status_code, 200, u)

    def test_batch_page_shows_workflow_stepper_and_fabric(self):
        batch = prod.create_batch(business=self.b, user=self.prodn, product=self.shirt, start_date=self.today, client=self.cust,
                                  plans=[(self.v28, 10, "Poly-cotton", 180)])
        h = self.c.get(f"/production/batches/{batch.pk}/").content.decode()
        self.assertIn("wf-steps", h)
        self.assertIn("Poly-cotton", h)
        self.assertIn("School A", h)

    def test_finance_overview_has_required_cards(self):
        h = self.c.get("/finance/").content.decode()
        for label in ("Total stock value", "Customer outstanding", "Supplier outstanding", "Weekly wages payable",
                      "Cash flow", "Where the money is"):
            self.assertIn(label, h)

    def test_party_delete_blocked_after_documents(self):
        from sales import services as sal
        self.stock_fg(self.v28, 10)
        sal.create_invoice(business=self.b, user=self.acct, customer_id=self.cust.pk, invoice_date=self.today,
                           lines=[{"variant_id": self.v28.pk, "quantity": 1, "rate": 10}])
        with self.assertRaises(DomainError):
            par.delete_party(business=self.b, user=self.owner, party_id=self.cust.pk)

    def test_unused_party_can_be_edited_and_deleted(self):
        p = par.create_party(business=self.b, user=self.acct, name="Temp Co", is_customer=True)
        par.update_party(business=self.b, user=self.acct, party_id=p.pk, name="Temp Renamed", is_customer=True)
        par.delete_party(business=self.b, user=self.owner, party_id=p.pk)

        batch = self.make_batch(q28=20)
        self.entry(batch, self.op_cut, "CUT", good=5)
        with self.assertRaises(DomainError):
            prod.delete_batch(business=self.b, user=self.owner, batch_id=batch.pk)

    def test_empty_batch_can_be_deleted(self):
        batch = self.make_batch(q28=20)
        prod.delete_batch(business=self.b, user=self.owner, batch_id=batch.pk)
        self.assertFalse(Batch.objects.filter(pk=batch.pk).exists())


class PaymentStatus(ERPTestCase):
    def test_status_thresholds(self):
        self.assertEqual(payment_status(100, 0), UNPAID)
        self.assertEqual(payment_status(100, D("40")), PARTIAL)
        self.assertEqual(payment_status(100, 100), PAID)
        self.assertEqual(payment_status(100, 120), PAID)
