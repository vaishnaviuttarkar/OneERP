from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from reports import reconciliation as rc


class DemoDataAndReports(TestCase):
    """Loads the demo business through the real services, then exercises reconciliation and every page."""

    @classmethod
    def setUpTestData(cls):
        call_command("load_demo_data", stdout=StringIO())

    def test_reconciliation_clean_on_demo_data(self):
        from core.models import Business
        b = Business.objects.get(slug="shree-ganesh-uniforms")
        for c in rc.run_all(b):
            self.assertTrue(c.ok, f"{c.name}: {c.issues}")

    def test_management_command_runs_and_exit_code_zero(self):
        out = StringIO()
        call_command("run_reconciliation", stdout=out)
        self.assertIn("All checks passed", out.getvalue())

    def test_owner_can_open_every_page(self):
        c = Client()
        self.assertTrue(c.login(username="owner", password="Demo@12345"))
        urls = ["/", "/parties/", "/catalogue/products/", "/purchasing/", "/inventory/", "/inventory/movements/", "/production/batches/", "/production/entries/",
                "/production/workers/", "/production/operations/", "/production/workflows/", "/production/rates/", "/payroll/", "/payroll/advances/",
                "/sales/challans/", "/sales/invoices/", "/finance/receipts/", "/finance/supplier-payments/", "/finance/expenses/", "/finance/cashbook/",
                "/reports/", "/reports/reconciliation/", "/accounts/users/", "/production/batches/new/", "/production/entries/new/", "/purchasing/new/",
                "/sales/invoices/new/", "/finance/receipts/new/", "/payroll/new/"]
        from reports.views import REPORTS
        urls += [f"/reports/{slug}/" for slug in REPORTS]
        for u in urls:
            self.assertEqual(c.get(u).status_code, 200, u)

    def test_detail_and_print_pages(self):
        from production.models import Batch
        from sales.models import DeliveryChallan, SalesInvoice
        from payroll.models import WageStatement
        c = Client()
        c.login(username="accounts", password="Demo@12345")
        urls = [f"/production/batches/{Batch.objects.first().pk}/", f"/sales/invoices/{SalesInvoice.objects.filter(status='posted').first().pk}/print/",
                f"/sales/challans/{DeliveryChallan.objects.first().pk}/print/", f"/payroll/{WageStatement.objects.filter(status='finalized').first().pk}/print/",
                f"/payroll/{WageStatement.objects.filter(status='finalized').first().pk}/"]
        for u in urls:
            self.assertEqual(c.get(u).status_code, 200, u)

    def test_production_user_sees_no_money_anywhere_in_production_pages(self):
        from production.models import Batch
        c = Client()
        c.login(username="production", password="Demo@12345")
        for u in ["/", "/production/batches/", f"/production/batches/{Batch.objects.first().pk}/", "/production/entries/", "/inventory/"]:
            r = c.get(u)
            self.assertEqual(r.status_code, 200, u)
            self.assertNotIn("₹", r.content.decode(), u)
        self.assertEqual(c.get("/inventory/movements/").status_code, 403)

    def test_finance_overview_values_come_from_database(self):
        # money figures moved from the dashboard to the Finance overview page
        from inventory.selectors import total_stock_value
        from core.models import Business
        b = Business.objects.get(slug="shree-ganesh-uniforms")
        c = Client()
        c.login(username="owner", password="Demo@12345")
        html = c.get("/finance/").content.decode()
        self.assertIn(f"{total_stock_value(b):.2f}", html.replace(",", ""))

    def test_dashboard_shows_production_insights_not_money(self):
        c = Client()
        c.login(username="owner", password="Demo@12345")
        html = c.get("/").content.decode()
        self.assertIn("Active batches", html)
        self.assertIn("Batches by status", html)
        self.assertNotIn("Cash flow", html)
        self.assertNotIn("Where the money is", html)
