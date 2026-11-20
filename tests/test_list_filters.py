import re
from decimal import Decimal as D

from django.test import Client

from finance import services as fin
from parties import services as par
from purchasing import services as pur
from sales import services as sal
from .base import ERPTestCase


class ListFilterTests(ERPTestCase):
    """Filters, search, pagination and sorting must compose on every list page."""

    def setUp(self):
        super().setUp()
        self.c = Client()
        self.c.login(username="owner", password="x")

    def rows(self, html, label):
        return len(re.findall(rf'<td data-label="{re.escape(label)}">', html))

    # ------------------------------------------------------------ parties
    def test_party_role_filter(self):
        h = self.c.get("/parties/", {"role": "customer"}).content.decode()
        self.assertIn("School A", h)
        self.assertNotIn("Supplier A", h)
        h = self.c.get("/parties/", {"role": "supplier"}).content.decode()
        self.assertIn("Supplier A", h)
        self.assertNotIn("School A", h)
        par.create_party(business=self.b, user=self.acct, name="Transporter")
        h = self.c.get("/parties/", {"role": "other"}).content.decode()
        self.assertIn("Transporter", h)
        self.assertNotIn("School A", h)

    def test_search_and_filter_combine(self):
        par.create_party(business=self.b, user=self.acct, name="School Beta", is_customer=True)
        h = self.c.get("/parties/", {"role": "customer", "q": "beta"}).content.decode()
        self.assertIn("School Beta", h)
        self.assertNotIn("School A<", h)
        self.assertNotIn("Supplier A", h)

    def test_pagination_keeps_filter_and_search(self):
        for i in range(25):
            par.create_party(business=self.b, user=self.acct, name=f"Cust {i:02d}", is_customer=True)
        p1 = self.c.get("/parties/", {"role": "customer"}).content.decode()
        self.assertEqual(self.rows(p1, "Name"), 20)
        self.assertIn("role=customer&page=2", p1)                # filter survives in the pager link
        p2 = self.c.get("/parties/", {"role": "customer", "page": 2}).content.decode()
        self.assertEqual(self.rows(p2, "Name"), 6)               # 25 + "School A"
        self.assertNotIn("Supplier A", p2)
        self.assertIn("role=customer&page=1", p2)

    def test_changing_a_filter_never_leaves_a_stale_page_param(self):
        # The form (and the HTMX request built from it) must not carry ?page= - a new filter starts at page 1.
        h = self.c.get("/parties/", {"role": "customer", "page": 2}).content.decode()
        form = h[h.index('<form class="toolbar"'):h.index("</form>", h.index('<form class="toolbar"'))]
        self.assertNotIn('name="page"', form)

    def test_sorting_composes_with_filter(self):
        par.create_party(business=self.b, user=self.acct, name="Alpha Co", is_customer=True)
        par.create_party(business=self.b, user=self.acct, name="Zulu Co", is_customer=True)
        asc = self.c.get("/parties/", {"role": "customer", "sort": "0", "dir": "asc"}).content.decode()
        desc = self.c.get("/parties/", {"role": "customer", "sort": "0", "dir": "desc"}).content.decode()
        self.assertLess(asc.index("Alpha Co"), asc.index("Zulu Co"))
        self.assertLess(desc.index("Zulu Co"), desc.index("Alpha Co"))
        self.assertNotIn("Supplier A", desc)
        self.assertIn('name="sort" value="0"', desc)             # sort is carried through the filter form
        self.assertIn("sort=0&amp;dir=asc", desc)                # header link toggles direction (HTML-escaped &)

    def test_invalid_sort_parameters_are_ignored(self):
        for bad in ({"sort": "abc"}, {"sort": "99"}, {"sort": "-1"}, {"sort": "0", "dir": "sideways"}):
            self.assertEqual(self.c.get("/parties/", bad).status_code, 200, bad)

    def test_numeric_columns_sort_numerically(self):
        self.stock_fabric(qty="5", cost="10")
        self.stock_fabric(qty="50", cost="10")
        h = self.c.get("/purchasing/", {"sort": "4", "dir": "desc"}).content.decode()     # Total column
        self.assertLess(h.index("₹500.00"), h.index("₹50.00"))                              # 500 before 50, not "50" > "500"

    # ------------------------------------------------------------ purchasing
    def test_purchase_status_filter(self):
        self.stock_fabric()                                                              # posted, supplier bill B1
        pur.create_bill(business=self.b, user=self.acct, supplier_id=self.sup.pk, supplier_bill_number="DRAFT-1", bill_date=self.today,
                        lines=[{"variant_id": self.fabric.pk, "quantity": 1, "unit_cost": 5}])
        d = self.c.get("/purchasing/", {"status": "draft"}).content.decode()
        self.assertIn("DRAFT-1", d)
        self.assertNotIn(">B1<", d)
        p = self.c.get("/purchasing/", {"status": "posted"}).content.decode()
        self.assertIn(">B1<", p)
        self.assertNotIn("DRAFT-1", p)

    # ------------------------------------------------------------ inventory
    def test_stock_kind_filter_and_search(self):
        self.stock_fabric()
        self.stock_fg(self.v28, 10)
        raw = self.c.get("/inventory/", {"kind": "raw"}).content.decode()
        self.assertIn("[FAB]", raw)
        self.assertNotIn("[SH28]", raw)
        fin_ = self.c.get("/inventory/", {"kind": "finished"}).content.decode()
        self.assertIn("[SH28]", fin_)
        self.assertNotIn("[FAB]", fin_)
        both = self.c.get("/inventory/", {"kind": "raw", "q": "sh28"}).content.decode()
        self.assertNotIn("[FAB]", both)
        self.assertNotIn("[SH28]", both)

    def test_movement_type_filter(self):
        self.stock_fabric()
        self.stock_fg(self.v28, 10)
        opening = self.c.get("/inventory/movements/", {"type": "opening"}).content.decode()
        self.assertIn('data-label="Type">Opening stock', opening)          # (the dropdown also lists every label)
        self.assertNotIn('data-label="Type">Purchase receipt', opening)
        purchase = self.c.get("/inventory/movements/", {"type": "purchase_receipt"}).content.decode()
        self.assertIn('data-label="Type">Purchase receipt', purchase)
        self.assertNotIn('data-label="Type">Opening stock', purchase)

    # ------------------------------------------------------------ production
    def test_batch_status_filter(self):
        batch = self.make_batch()
        self.assertIn(batch.number, self.c.get("/production/batches/", {"status": "planned"}).content.decode())
        self.assertNotIn(batch.number, self.c.get("/production/batches/", {"status": "in_progress"}).content.decode())

    # ------------------------------------------------------------ sales
    def test_invoice_and_challan_status_filters(self):
        self.stock_fg(self.v28, 50)
        ch = sal.create_challan(business=self.b, user=self.acct, customer_id=self.cust.pk, challan_date=self.today, lines=[{"variant_id": self.v28.pk, "quantity": 5}])
        posted = sal.post_challan(business=self.b, user=self.acct, challan_id=ch.pk)
        draft = sal.create_challan(business=self.b, user=self.acct, customer_id=self.cust.pk, challan_date=self.today, lines=[{"variant_id": self.v28.pk, "quantity": 1}])
        h = self.c.get("/sales/challans/", {"status": "posted"}).content.decode()
        self.assertIn(posted.number, h)
        self.assertNotIn(f"Draft #{draft.pk}", h)
        h = self.c.get("/sales/challans/", {"status": "draft"}).content.decode()
        self.assertIn(f"Draft #{draft.pk}", h)
        self.assertNotIn(posted.number, h)
        inv = sal.create_invoice(business=self.b, user=self.acct, customer_id=self.cust.pk, invoice_date=self.today, lines=[{"variant_id": self.v28.pk, "quantity": 2, "rate": 10}])
        sal.post_invoice(business=self.b, user=self.acct, invoice_id=inv.pk)
        self.assertNotIn("INV/", self.c.get("/sales/invoices/", {"status": "draft"}).content.decode())
        self.assertIn("INV/", self.c.get("/sales/invoices/", {"status": "posted"}).content.decode())

    # ------------------------------------------------------------ finance
    def test_cashbook_account_filter(self):
        fin.create_expense(business=self.b, user=self.acct, category="rent", amount=D("100"), account_id=self.cash.pk, expense_date=self.today)
        fin.create_expense(business=self.b, user=self.acct, category="misc", amount=D("200"), account_id=self.bank.pk, expense_date=self.today)
        h = self.c.get("/finance/cashbook/", {"account": self.cash.pk}).content.decode()
        self.assertIn("Rent", h)
        self.assertNotIn("Miscellaneous", h)
        h = self.c.get("/finance/cashbook/", {"account": self.bank.pk}).content.decode()
        self.assertIn("Miscellaneous", h)
        self.assertNotIn("Rent", h)

    # ------------------------------------------------------------ markup
    def test_filter_form_markup_is_correct(self):
        h = self.c.get("/parties/").content.decode()
        self.assertIn('hx-get="/parties/"', h)                   # never an empty hx-get (it re-sent stale parameters)
        self.assertNotIn('hx-get=""', h)
        self.assertIn('hx-swap="outerHTML"', h)                  # no nested #results element
        self.assertEqual(h.count('id="results"'), 1)
        self.assertIn('action="/parties/"', h)                   # works without JavaScript too

    def test_selected_filter_option_is_preserved(self):
        h = self.c.get("/parties/", {"role": "supplier"}).content.decode()
        self.assertRegex(h, r'<option value="supplier"\s+selected>')

    def test_reset_link_only_when_filtering(self):
        self.assertNotIn(">Reset<", self.c.get("/parties/").content.decode())
        self.assertIn(">Reset<", self.c.get("/parties/", {"role": "customer"}).content.decode())


class ResponsiveMarkupTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.c = Client()
        self.c.login(username="owner", password="x")

    def test_viewport_and_mobile_navigation_present(self):
        h = self.c.get("/").content.decode()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', h)
        self.assertIn("menu-open", h)                            # hamburger toggles the drawer
        self.assertIn('class="icon-btn menu-btn"', h)

    def test_list_tables_collapse_into_labelled_cards(self):
        h = self.c.get("/parties/").content.decode()
        self.assertIn("table table-hover align-middle stack", h)
        for label in ("Name", "Role", "Phone", "Status"):
            self.assertIn(f'data-label="{label}"', h)

    def test_detail_and_form_tables_are_labelled(self):
        from catalogue.models import ProductVariant
        h = self.c.get("/purchasing/new/").content.decode()
        self.assertIn('data-label="Quantity"', h)
        self.assertIn("table table-sm mb-0 stack", h)

    def test_mobile_css_rules_exist(self):
        import pathlib
        css = (pathlib.Path(__file__).resolve().parent.parent / "static" / "css" / "app.css").read_text()
        self.assertIn("@media(max-width:767.98px)", css)
        self.assertIn("content:attr(data-label)", css)
        self.assertIn(".head-actions", css)

    def test_public_page_is_responsive_markup(self):
        h = Client().get("/contact/").content.decode()
        self.assertIn('name="viewport"', h)
        self.assertIn("contact-shell", h)                 # info panel + form card layout
        self.assertIn("contact-form", h)
        self.assertIn("pub-wrap", h)

    def test_every_main_page_renders_for_each_role(self):
        pages = {"owner": ["/", "/parties/", "/purchasing/", "/inventory/", "/production/batches/", "/payroll/", "/sales/invoices/",
                           "/finance/cashbook/", "/enquiries/", "/reports/", "/reports/reconciliation/"],
                 "acct": ["/", "/parties/", "/enquiries/", "/reports/"], "prod": ["/", "/production/batches/", "/inventory/", "/reports/"]}
        for user, urls in pages.items():
            c = Client()
            c.login(username=user, password="x")
            for u in urls:
                self.assertEqual(c.get(u).status_code, 200, f"{user} {u}")
