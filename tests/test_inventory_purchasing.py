from decimal import Decimal as D

from django.db import IntegrityError, transaction

from core.exceptions import DomainError, ImmutableRecordError, InsufficientStock
from inventory.models import StockBalance, StockMovement
from inventory.services import post_movement
from purchasing import services as pur
from purchasing.models import PurchaseBill
from .base import ERPTestCase


class Purchasing(ERPTestCase):
    def test_post_bill_creates_movement_and_excludes_tax(self):
        bill = self.stock_fabric(qty="10", cost="118", tax="180")     # line 1180, tax 180 -> stock cost 1000
        mv = StockMovement.objects.get(variant=self.fabric)
        self.assertEqual((mv.quantity, mv.value, mv.movement_type), (D("10"), D("1000.00"), "purchase_receipt"))
        self.assertEqual(StockBalance.objects.get(variant=self.fabric).value, D("1000.00"))
        self.assertTrue(bill.number.startswith("PB/"))

    def test_supplier_bill_number_unique_per_supplier(self):
        self.stock_fabric()
        with self.assertRaises(DomainError):
            pur.create_bill(business=self.b, user=self.acct, supplier_id=self.sup.pk, supplier_bill_number="B1", bill_date=self.today,
                            lines=[{"variant_id": self.fabric.pk, "quantity": 1, "unit_cost": 1}])

    def test_posted_bill_immutable_and_not_deletable_draft_deletable(self):
        bill = self.stock_fabric()
        with self.assertRaises(DomainError):
            pur.delete_draft_bill(business=self.b, user=self.acct, bill_id=bill.pk)
        bill.supplier_bill_number = "changed"
        with self.assertRaises(ImmutableRecordError):
            bill.save()
        d = pur.create_bill(business=self.b, user=self.acct, supplier_id=self.sup.pk, supplier_bill_number="D1", bill_date=self.today,
                            lines=[{"variant_id": self.fabric.pk, "quantity": 1, "unit_cost": 5}])
        pur.delete_draft_bill(business=self.b, user=self.acct, bill_id=d.pk)
        self.assertFalse(PurchaseBill.objects.filter(pk=d.pk).exists())

    def test_customer_only_party_cannot_be_supplier(self):
        with self.assertRaises(DomainError):
            pur.create_bill(business=self.b, user=self.acct, supplier_id=self.cust.pk, supplier_bill_number="X", bill_date=self.today,
                            lines=[{"variant_id": self.fabric.pk, "quantity": 1, "unit_cost": 1}])


class Inventory(ERPTestCase):
    def test_weighted_average_recalculated_on_receipt(self):
        self.stock_fabric(qty="10", cost="10")      # value 100
        self.stock_fabric(qty="10", cost="20")      # value 200 -> avg 15
        bal = StockBalance.objects.get(variant=self.fabric)
        self.assertEqual((bal.quantity, bal.value), (D("20"), D("300.00")))
        mv = post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-4"), user=self.prodn)
        self.assertEqual(mv.value, D("-60.00"))      # 4 x 15, fixed at issue time

    def test_issue_cost_rounding_half_up_and_final_issue_exact(self):
        self.stock_fabric(qty="3", cost="10.01")      # value 30.03, avg 10.01
        a = post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-1"), user=self.prodn)
        self.assertEqual(a.value, D("-10.01"))
        post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-1"), user=self.prodn)
        last = post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-1"), user=self.prodn)
        self.assertEqual(StockBalance.objects.get(variant=self.fabric).value, D("0.00"))
        self.assertEqual(sum(m.value for m in StockMovement.objects.filter(variant=self.fabric)), D("0.00"))
        self.assertEqual(last.value, D("-10.01"))

    def test_half_up_rounding_boundary(self):
        self.stock_fabric(qty="8", cost="1.25")       # value 10.00 (8 x 1.25)
        mv = post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-3"), user=self.prodn)
        self.assertEqual(mv.value, D("-3.75"))
        self.stock_fabric(qty="1", cost="0.01")
        mv2 = post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-1"), user=self.prodn)
        self.assertEqual(mv2.value.quantize(D("0.01")), mv2.value)

    def test_negative_stock_prevented(self):
        self.stock_fabric(qty="5")
        with self.assertRaises(InsufficientStock):
            post_movement(business=self.b, variant=self.fabric, movement_type="material_issue", quantity=D("-5.001"), user=self.prodn)
        self.assertEqual(StockBalance.objects.get(variant=self.fabric).quantity, D("5"))

    def test_movements_are_immutable(self):
        self.stock_fabric()
        mv = StockMovement.objects.first()
        mv.note = "x"
        with self.assertRaises(ImmutableRecordError):
            mv.save()
        with self.assertRaises(ImmutableRecordError):
            StockMovement.objects.all().delete()
        with self.assertRaises(ImmutableRecordError):
            mv.delete()

    def test_database_blocks_negative_balance(self):
        self.stock_fabric(qty="1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockBalance.objects.filter(variant=self.fabric).update(quantity=D("-1"))

    def test_no_backdated_stock_documents(self):
        from datetime import timedelta
        with self.assertRaises(DomainError):
            post_movement(business=self.b, variant=self.fabric, movement_type="opening", quantity=D("1"), user=self.acct,
                          business_date=self.today - timedelta(days=1), value=D("1"))

    def test_balance_matches_journal_in_reconciliation(self):
        from reports.reconciliation import inventory_quantity, inventory_value
        self.stock_fabric(qty="10", cost="7")
        post_movement(business=self.b, variant=self.fabric, movement_type="damage", quantity=D("-2"), user=self.acct)
        self.assertTrue(inventory_quantity(self.b).ok and inventory_value(self.b).ok)
