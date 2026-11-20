import threading
from decimal import Decimal as D

from django.db import IntegrityError, connection, transaction
from django.test import Client, TransactionTestCase

from accounts.models import User
from core.exceptions import DomainError, IdempotencyConflict, ImmutableRecordError
from core.models import AuditEvent, DocumentSequence, IdempotencyKey
from core.services import next_number, record_audit, run_idempotent
from parties import services as par
from catalogue import services as cat
from .base import ERPTestCase


class AuthAndRoles(ERPTestCase):
    def test_login_required(self):
        self.assertEqual(Client().get("/").status_code, 302)

    def test_owner_can_login_and_see_dashboard(self):
        c = Client()
        self.assertTrue(c.login(username="owner", password="x"))
        self.assertEqual(c.get("/").status_code, 200)

    def test_production_blocked_from_financial_pages(self):
        c = Client()
        c.login(username="prod", password="x")
        for url in ("/payroll/", "/production/rates/", "/finance/cashbook/", "/sales/invoices/", "/purchasing/", "/reports/reconciliation/", "/reports/cash-flow/"):
            self.assertEqual(c.get(url).status_code, 403, url)
        self.assertEqual(c.get("/production/batches/").status_code, 200)

    def test_production_api_never_exposes_rates(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(self.prodn)
        self.assertEqual(c.post("/api/wage-statements/", {}, format="json").status_code, 403)
        self.assertEqual(c.post("/api/receipts/", {}, format="json").status_code, 403)
        self.assertEqual(c.get("/api/reconciliation/").status_code, 403)
        for row in c.get("/api/stock/").json():
            self.assertNotIn("value", row)

    def test_accounts_cannot_open_reconciliation_or_users(self):
        c = Client()
        c.login(username="acct", password="x")
        self.assertEqual(c.get("/reports/reconciliation/").status_code, 403)
        self.assertEqual(c.get("/accounts/users/").status_code, 403)

    def test_inactive_user_cannot_login(self):
        User.objects.filter(username="acct").update(is_active=False)
        self.assertFalse(Client().login(username="acct", password="x"))


class PartiesAndProducts(ERPTestCase):
    def test_party_name_case_insensitive_unique_and_trimmed(self):
        with self.assertRaises(DomainError):
            par.create_party(business=self.b, user=self.acct, name="  school   a ", is_customer=True)

    def test_other_party_not_usable_and_inactive_blocked(self):
        other = par.create_party(business=self.b, user=self.acct, name="Transporter")
        with self.assertRaises(DomainError):
            par.require_party(self.b, other.pk, customer=True)
        par.set_party_active(business=self.b, user=self.acct, party_id=self.cust.pk, active=False)
        with self.assertRaises(DomainError):
            par.require_party(self.b, self.cust.pk, customer=True)

    def test_finished_good_requires_workflow(self):
        with self.assertRaises(DomainError):
            cat.create_product(business=self.b, user=self.acct, name="Skirt", kind="finished")

    def test_raw_material_has_exactly_one_blank_variant(self):
        with self.assertRaises(DomainError):
            cat.add_variant(business=self.b, user=self.acct, product=self.fabric.product, sku="FAB2")
        p = cat.create_product(business=self.b, user=self.acct, name="Thread", kind="accessory")
        with self.assertRaises(DomainError):
            cat.add_variant(business=self.b, user=self.acct, product=p, sku="TH", size="L")

    def test_sku_unique_per_business(self):
        with self.assertRaises(DomainError):
            cat.add_variant(business=self.b, user=self.acct, product=self.shirt, sku="sh28", size="32")


class NumberingAuditImmutability(ERPTestCase):
    def test_document_numbers_sequential_and_per_type(self):
        a, b2 = next_number(self.b, "receipt"), next_number(self.b, "receipt")
        self.assertNotEqual(a, b2)
        self.assertTrue(a.endswith("00001") and b2.endswith("00002"))
        self.assertTrue(next_number(self.b, "expense").endswith("00001"))
        self.assertEqual(DocumentSequence.objects.filter(business=self.b, doc_type="receipt").count(), 1)

    def test_financial_year_april_to_march(self):
        from datetime import date
        self.assertIn("/2025-26/", next_number(self.b, "receipt", date(2026, 3, 31)))
        self.assertIn("/2026-27/", next_number(self.b, "receipt", date(2026, 4, 1)))

    def test_audit_event_append_only(self):
        e = record_audit(business=self.b, user=self.owner, action="x", entity="E", entity_id=1)
        e.action = "y"
        with self.assertRaises(ImmutableRecordError):
            e.save()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.filter(pk=e.pk).delete()
        with self.assertRaises(ImmutableRecordError):
            AuditEvent.objects.filter(pk=e.pk).update(action="z")

    def test_services_write_audit_events(self):
        self.assertTrue(AuditEvent.objects.filter(business=self.b, action="party.create").exists())


class Idempotency(ERPTestCase):
    def _run(self, key, payload, counter):
        def func():
            counter.append(1)
            return {"n": len(counter)}
        return run_idempotent(business=self.b, user=self.acct, key=key, action="demo", payload=payload, func=func)

    def test_replay_returns_original(self):
        calls = []
        r1, rep1 = self._run("k1", {"a": 1}, calls)
        r2, rep2 = self._run("k1", {"a": 1}, calls)
        self.assertEqual((r1, rep1, r2, rep2), ({"n": 1}, False, {"n": 1}, True))
        self.assertEqual(len(calls), 1)

    def test_same_key_different_payload_conflicts(self):
        calls = []
        self._run("k2", {"a": 1}, calls)
        with self.assertRaises(IdempotencyConflict):
            self._run("k2", {"a": 2}, calls)

    def test_rolled_back_operation_does_not_consume_key(self):
        def boom():
            raise DomainError("nope")
        with self.assertRaises(DomainError):
            run_idempotent(business=self.b, user=self.acct, key="k3", action="demo", payload={}, func=boom)
        self.assertFalse(IdempotencyKey.objects.filter(key="k3").exists())
        r, rep = self._run("k3", {}, [])
        self.assertFalse(rep)

    def test_api_conflict_is_http_409(self):
        from rest_framework.test import APIClient
        self.stock_fabric()
        c = APIClient()
        c.force_authenticate(self.acct)
        body = {"supplier_id": self.sup.pk, "supplier_bill_number": "API-1", "bill_date": str(self.today),
                "lines": [{"variant_id": self.fabric.pk, "quantity": "5", "unit_cost": "10"}]}
        r1 = c.post("/api/purchase-bills/", body, format="json", headers={"Idempotency-Key": "abc"})
        self.assertEqual(r1.status_code, 201)
        r2 = c.post("/api/purchase-bills/", body, format="json", headers={"Idempotency-Key": "abc"})
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2["Idempotent-Replayed"], "true")
        self.assertEqual(r2.json()["id"], r1.json()["id"])
        body2 = dict(body, supplier_bill_number="API-2")
        self.assertEqual(c.post("/api/purchase-bills/", body2, format="json", headers={"Idempotency-Key": "abc"}).status_code, 409)


class ConcurrentIdempotency(TransactionTestCase):
    """Uses real threads/connections, hence TransactionTestCase."""
    def test_concurrent_identical_requests_run_once(self):
        from core.models import Business
        b = Business.objects.create(name="C", slug="c")
        u = User.objects.create_user("u", password="x", role="accounts")
        calls, results = [], []

        def worker():
            try:
                results.append(run_idempotent(business=b, user=u, key="same", action="a", payload={"x": 1},
                                              func=lambda: (calls.append(1), {"ok": True})[1]))
            finally:
                connection.close()
        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 4)
        self.assertEqual(IdempotencyKey.objects.filter(key="same").count(), 1)
