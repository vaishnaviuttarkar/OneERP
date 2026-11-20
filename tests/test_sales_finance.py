from decimal import Decimal as D

from core.exceptions import DomainError, ImmutableRecordError, InsufficientStock
from finance import services as fin
from finance.models import CashBookEntry, Receipt
from inventory.models import StockBalance, StockMovement
from sales import services as sal
from sales.models import SalesInvoice
from .base import ERPTestCase


class SalesBase(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.stock_fg(self.v28, 100)

    def fg(self):
        return StockBalance.objects.get(variant=self.v28).quantity

    def posted_challan(self, qty=40):
        ch = sal.create_challan(business=self.b, user=self.acct, customer_id=self.cust.pk, challan_date=self.today, lines=[{"variant_id": self.v28.pk, "quantity": qty}])
        return sal.post_challan(business=self.b, user=self.acct, challan_id=ch.pk)

    def invoice(self, lines, post=True):
        inv = sal.create_invoice(business=self.b, user=self.acct, customer_id=self.cust.pk, invoice_date=self.today, lines=lines)
        return sal.post_invoice(business=self.b, user=self.acct, invoice_id=inv.pk) if post else inv


class Challans(SalesBase):
    def test_challan_dispatches_stock_exactly_once(self):
        ch = self.posted_challan(40)
        self.assertEqual(self.fg(), D("60"))
        self.assertTrue(ch.number.startswith("DC/"))
        with self.assertRaises(DomainError):
            sal.post_challan(business=self.b, user=self.acct, challan_id=ch.pk)
        self.assertEqual(StockMovement.objects.filter(movement_type="dispatch").count(), 1)

    def test_invoice_from_challan_does_not_dispatch_again(self):
        ch = self.posted_challan(40)
        cl = ch.lines.get()
        self.invoice([{"variant_id": self.v28.pk, "quantity": 40, "rate": 100, "challan_line_id": cl.pk}])
        self.assertEqual(self.fg(), D("60"))
        self.assertEqual(StockMovement.objects.filter(movement_type="dispatch").count(), 1)

    def test_partial_invoicing_and_cumulative_limit(self):
        cl = self.posted_challan(40).lines.get()
        self.invoice([{"variant_id": self.v28.pk, "quantity": 25, "rate": 100, "challan_line_id": cl.pk}])
        with self.assertRaises(DomainError):
            self.invoice([{"variant_id": self.v28.pk, "quantity": 16, "rate": 100, "challan_line_id": cl.pk}])
        self.invoice([{"variant_id": self.v28.pk, "quantity": 15, "rate": 100, "challan_line_id": cl.pk}])
        with self.assertRaises(DomainError):
            self.invoice([{"variant_id": self.v28.pk, "quantity": 1, "rate": 100, "challan_line_id": cl.pk}])

    def test_invoice_combines_challan_and_direct_lines(self):
        cl = self.posted_challan(10).lines.get()
        inv = self.invoice([{"variant_id": self.v28.pk, "quantity": 10, "rate": 100, "challan_line_id": cl.pk},
                            {"variant_id": self.v28.pk, "quantity": 5, "rate": 100}])
        self.assertEqual(self.fg(), D("85"))      # 100 - 10 (challan) - 5 (direct); no double dispatch
        self.assertEqual(inv.lines.count(), 2)

    def test_direct_invoice_dispatches_on_post_and_draft_does_not(self):
        d = self.invoice([{"variant_id": self.v28.pk, "quantity": 7, "rate": 10}], post=False)
        self.assertEqual(self.fg(), D("100"))
        sal.post_invoice(business=self.b, user=self.acct, invoice_id=d.pk)
        self.assertEqual(self.fg(), D("93"))

    def test_no_negative_stock_on_dispatch(self):
        with self.assertRaises(InsufficientStock):
            self.invoice([{"variant_id": self.v28.pk, "quantity": 101, "rate": 10}])
        self.assertEqual(self.fg(), D("100"))

    def test_integer_pieces_only(self):
        with self.assertRaises(DomainError):
            self.invoice([{"variant_id": self.v28.pk, "quantity": D("1.5"), "rate": 10}], post=False)

    def test_total_rounded_to_whole_rupee(self):
        inv = self.invoice([{"variant_id": self.v28.pk, "quantity": 3, "rate": D("10.50")}])      # 31.50 -> 32
        self.assertEqual((inv.subtotal, inv.round_off, inv.total), (D("31.50"), D("0.50"), D("32.00")))
        inv2 = self.invoice([{"variant_id": self.v28.pk, "quantity": 1, "rate": D("10.49")}])
        self.assertEqual(inv2.total, D("10.00"))

    def test_posted_invoice_immutable(self):
        inv = self.invoice([{"variant_id": self.v28.pk, "quantity": 1, "rate": 10}])
        inv.notes = "x"
        with self.assertRaises(ImmutableRecordError):
            inv.save()
        with self.assertRaises(DomainError):
            sal.delete_draft_invoice(business=self.b, user=self.acct, invoice_id=inv.pk)

    def test_cancel_invoice_releases_quantity_and_reverses_direct_dispatch(self):
        cl = self.posted_challan(10).lines.get()
        i1 = self.invoice([{"variant_id": self.v28.pk, "quantity": 10, "rate": 10, "challan_line_id": cl.pk}])
        with self.assertRaises(DomainError):       # challan still has active invoiced quantity
            sal.cancel_challan(business=self.b, user=self.acct, challan_id=cl.challan_id)
        sal.cancel_invoice(business=self.b, user=self.acct, invoice_id=i1.pk)
        self.invoice([{"variant_id": self.v28.pk, "quantity": 10, "rate": 10, "challan_line_id": cl.pk}])   # released
        i2 = self.invoice([{"variant_id": self.v28.pk, "quantity": 4, "rate": 10}])
        self.assertEqual(self.fg(), D("86"))
        sal.cancel_invoice(business=self.b, user=self.acct, invoice_id=i2.pk)
        self.assertEqual(self.fg(), D("90"))

    def test_cancel_challan_reverses_dispatch_when_allowed(self):
        ch = self.posted_challan(30)
        sal.cancel_challan(business=self.b, user=self.acct, challan_id=ch.pk)
        self.assertEqual(self.fg(), D("100"))
        self.assertEqual(sal.DeliveryChallan.objects.get(pk=ch.pk).status, "cancelled")
        with self.assertRaises(DomainError):
            sal.cancel_challan(business=self.b, user=self.acct, challan_id=ch.pk)

    def test_cancelled_invoice_number_never_reused(self):
        i1 = self.invoice([{"variant_id": self.v28.pk, "quantity": 1, "rate": 10}])
        sal.cancel_invoice(business=self.b, user=self.acct, invoice_id=i1.pk)
        i2 = self.invoice([{"variant_id": self.v28.pk, "quantity": 1, "rate": 10}])
        self.assertNotEqual(i1.number, i2.number)

    def test_challan_for_other_customer_cannot_be_invoiced(self):
        from parties import services as par
        cl = self.posted_challan(5).lines.get()
        other = par.create_party(business=self.b, user=self.acct, name="School B", is_customer=True)
        with self.assertRaises(DomainError):
            sal.create_invoice(business=self.b, user=self.acct, customer_id=other.pk, invoice_date=self.today,
                               lines=[{"variant_id": self.v28.pk, "quantity": 1, "rate": 1, "challan_line_id": cl.pk}])


class Receipts(SalesBase):
    def setUp(self):
        super().setUp()
        self.inv = self.invoice([{"variant_id": self.v28.pk, "quantity": 10, "rate": 100}])   # 1000

    def test_receipt_allocation_and_cashbook(self):
        r = fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.bank.pk, amount=D("600"), receipt_date=self.today,
                               allocations=[{"doc_id": self.inv.pk, "amount": D("400")}])
        self.assertEqual(r.allocations.get().amount, D("400.00"))
        self.assertEqual(CashBookEntry.objects.get(receipt=r).direction, "in")
        fin.allocate_receipt(business=self.b, user=self.acct, receipt_id=r.pk, allocations=[{"doc_id": self.inv.pk, "amount": D("200")}])
        with self.assertRaises(DomainError):        # receipt fully allocated now
            fin.allocate_receipt(business=self.b, user=self.acct, receipt_id=r.pk, allocations=[{"doc_id": self.inv.pk, "amount": D("1")}])

    def test_allocation_cannot_exceed_outstanding_or_receipt(self):
        with self.assertRaises(DomainError):
            fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.cash.pk, amount=D("2000"), receipt_date=self.today,
                               allocations=[{"doc_id": self.inv.pk, "amount": D("1001")}])
        with self.assertRaises(DomainError):
            fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.cash.pk, amount=D("100"), receipt_date=self.today,
                               allocations=[{"doc_id": self.inv.pk, "amount": D("101")}])
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(CashBookEntry.objects.count(), 0)     # failed receipt left no cashbook entry

    def test_invoice_with_allocation_cannot_be_cancelled_and_receipts_immutable(self):
        r = fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.cash.pk, amount=D("100"), receipt_date=self.today,
                               allocations=[{"doc_id": self.inv.pk, "amount": D("100")}])
        with self.assertRaises(DomainError):
            sal.cancel_invoice(business=self.b, user=self.acct, invoice_id=self.inv.pk)
        r.amount = D("1")
        with self.assertRaises(ImmutableRecordError):
            r.save()

    def test_cannot_allocate_to_cancelled_or_other_customers_invoice(self):
        i2 = self.invoice([{"variant_id": self.v28.pk, "quantity": 1, "rate": 10}])
        sal.cancel_invoice(business=self.b, user=self.acct, invoice_id=i2.pk)
        with self.assertRaises(DomainError):
            fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.cash.pk, amount=D("10"), receipt_date=self.today,
                               allocations=[{"doc_id": i2.pk, "amount": D("10")}])

    def test_receivable_derived_from_invoices_minus_allocations(self):
        from sales.selectors import total_receivable
        fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.cash.pk, amount=D("300"), receipt_date=self.today,
                           allocations=[{"doc_id": self.inv.pk, "amount": D("300")}])
        self.assertEqual(total_receivable(self.b), D("700"))

    def test_reconciliation_sales_and_cash(self):
        from reports import reconciliation as rc
        fin.create_receipt(business=self.b, user=self.acct, customer_id=self.cust.pk, account_id=self.cash.pk, amount=D("300"), receipt_date=self.today,
                           allocations=[{"doc_id": self.inv.pk, "amount": D("300")}])
        self.assertTrue(rc.invoice_allocations(self.b).ok and rc.challan_invoicing(self.b).ok and rc.cashbook(self.b).ok)


class SupplierSide(ERPTestCase):
    def test_supplier_payment_allocation_and_payable(self):
        from purchasing.selectors import total_payable
        bill = self.stock_fabric(qty="10", cost="100")     # 1000
        with self.assertRaises(DomainError):
            fin.create_supplier_payment(business=self.b, user=self.acct, supplier_id=self.sup.pk, account_id=self.bank.pk, amount=D("2000"), payment_date=self.today,
                                        allocations=[{"doc_id": bill.pk, "amount": D("1001")}])
        p = fin.create_supplier_payment(business=self.b, user=self.acct, supplier_id=self.sup.pk, account_id=self.bank.pk, amount=D("400"), payment_date=self.today,
                                        allocations=[{"doc_id": bill.pk, "amount": D("400")}])
        self.assertEqual(total_payable(self.b), D("600"))
        self.assertEqual(CashBookEntry.objects.get(supplier_payment=p).direction, "out")
        with self.assertRaises(DomainError):          # customer-only party cannot receive supplier payments
            fin.create_supplier_payment(business=self.b, user=self.acct, supplier_id=self.cust.pk, account_id=self.bank.pk, amount=D("1"), payment_date=self.today)

    def test_expense_posts_cashbook_and_is_immutable(self):
        e = fin.create_expense(business=self.b, user=self.acct, category="rent", amount=D("5000"), account_id=self.cash.pk, expense_date=self.today)
        self.assertEqual(CashBookEntry.objects.get(expense=e).amount, D("5000.00"))
        e.amount = D("1")
        with self.assertRaises(ImmutableRecordError):
            e.save()

    def test_cash_balance(self):
        from finance.selectors import cash_position
        self.cash.opening_balance = D("1000")
        self.cash.save()
        fin.create_expense(business=self.b, user=self.acct, category="misc", amount=D("250"), account_id=self.cash.pk, expense_date=self.today)
        self.assertEqual(cash_position(self.b), D("750"))

    def test_cashbook_entry_needs_exactly_one_source(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            CashBookEntry.objects.create(business=self.b, account=self.cash, entry_date=self.today, direction="in", amount=D("1"))


class BusinessScoping(ERPTestCase):
    def test_other_business_data_is_not_reachable(self):
        from core.models import Business, FinancialAccount
        other = Business.objects.create(name="Other", slug="other")
        oacc = FinancialAccount.objects.create(business=other, name="Cash", kind="cash")
        with self.assertRaises(DomainError):
            fin.create_expense(business=self.b, user=self.acct, category="misc", amount=D("1"), account_id=oacc.pk, expense_date=self.today)
        with self.assertRaises(DomainError):
            from parties import services as par
            par.require_party(other, self.cust.pk, customer=True)
