from datetime import timedelta
from decimal import Decimal as D

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalogue import services as cat
from catalogue.models import ProductVariant
from core.models import Business, BusinessSettings, FinancialAccount
from finance import services as fin
from inventory import services as inv
from parties import services as par
from payroll import services as pay
from production import services as prod
from production.models import StageDefinition, StageOutcome, WorkEntry
from purchasing import services as pur
from sales import services as sal

DEMO_PASSWORD = "Demo@12345"   # development only - change before any real deployment


class Command(BaseCommand):
    help = "Create a realistic demo business using the real service functions (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default=DEMO_PASSWORD)

    @transaction.atomic
    def handle(self, *args, **o):
        if Business.objects.filter(slug="shree-ganesh-uniforms").exists():
            raise CommandError("Demo business already exists. Reset the dev database to reload (see README).")
        today = timezone.localdate()
        d = lambda n: today - timedelta(days=n)

        b = Business.objects.create(name="Shree Ganesh Uniforms", legal_name="Shree Ganesh Uniforms", slug="shree-ganesh-uniforms")
        BusinessSettings.objects.create(business=b, address="Plot 14, MIDC, Dombivli (E), Maharashtra", phone="+91 98200 00000",
                                        invoice_footer="Thank you for your business. Goods once sold will not be taken back.")
        cash = FinancialAccount.objects.create(business=b, name="Cash in Hand", kind="cash", opening_balance=D("50000"))
        bank = FinancialAccount.objects.create(business=b, name="HDFC Current Account", kind="bank", opening_balance=D("250000"))
        owner = User.objects.create_user("owner", password=o["password"], role=User.OWNER, first_name="Owner", email="owner@example.com")
        acct = User.objects.create_user("accounts", password=o["password"], role=User.ACCOUNTS, first_name="Accounts")
        prodn = User.objects.create_user("production", password=o["password"], role=User.PRODUCTION, first_name="Production")

        # stages / workflow
        st = {c: StageDefinition.objects.create(business=b, name=n, code=c, default_outcome_source=s, sequence=seq) for c, n, s, seq in [
            ("CUT", "Cutting", "work_entry", 10), ("STITCH", "Stitching", "work_entry", 20), ("IRON", "Ironing", "work_entry", 30),
            ("QC", "Quality Inspection", "inspection", 40), ("FG", "Finished Goods", "none", 50)]}
        wf = prod.create_workflow(business=b, user=owner, name="Standard uniform workflow",
                                  steps=[(st[c], st[c].default_outcome_source) for c in ("CUT", "STITCH", "IRON", "QC", "FG")])

        # catalogue
        shirt = cat.create_product(business=b, user=acct, name="School Shirt", kind="finished", default_workflow=wf)
        trouser = cat.create_product(business=b, user=acct, name="School Trousers", kind="finished", default_workflow=wf)
        sv = {s: cat.add_variant(business=b, user=acct, product=shirt, sku=f"SHIRT-{s}", size=s, colour="White") for s in ("28", "30", "32")}
        tv = {s: cat.add_variant(business=b, user=acct, product=trouser, sku=f"TROUSER-{s}", size=s, colour="Navy") for s in ("30", "32")}
        mats = {}
        for name, sku, unit in [("Shirting Fabric", "FAB-SHIRT", "m"), ("Trouser Fabric", "FAB-TROUSER", "m"), ("Buttons", "BTN-001", "pcs"),
                                ("Zippers", "ZIP-001", "pcs"), ("Sewing Thread", "THR-001", "roll")]:
            p = cat.create_product(business=b, user=acct, name=name, kind="raw" if "Fabric" in name else "accessory")
            mats[sku] = cat.add_variant(business=b, user=acct, product=p, sku=sku, unit=unit)

        # parties
        cust = [par.create_party(business=b, user=acct, name=n, is_customer=True, phone=ph) for n, ph in
                [("St. Xavier's High School", "022-2000001"), ("Green Valley Public School", "022-2000002"), ("Little Angels Academy", "022-2000003")]]
        sup1 = par.create_party(business=b, user=acct, name="Mehta Textiles", is_supplier=True, phone="022-3000001")
        sup2 = par.create_party(business=b, user=acct, name="Sai Trimmings", is_supplier=True, phone="022-3000002")
        par.create_party(business=b, user=acct, name="Dombivli Transport Co.")   # 'Other' party

        # workers / operations / rates
        w = [prod.create_worker(business=b, user=prodn, name=n, code=c) for n, c in
             [("Ramesh Patil", "W001"), ("Sunita Shinde", "W002"), ("Anil More", "W003"), ("Meena Gaikwad", "W004")]]
        op = {
            "cut": prod.create_operation(business=b, user=prodn, name="Cutting", stage=st["CUT"]),
            "stitch": prod.create_operation(business=b, user=prodn, name="Stitching", stage=st["STITCH"]),
            "rework": prod.create_operation(business=b, user=prodn, name="Stitching Rework", stage=st["STITCH"], is_rework=True),
            "iron": prod.create_operation(business=b, user=prodn, name="Ironing", stage=st["IRON"]),
            "button": prod.create_operation(business=b, user=prodn, name="Button Fixing", is_payroll_only=True),
        }
        base = d(30)
        for key, rate in [("cut", 8), ("stitch", 45), ("rework", 25), ("iron", 10), ("button", 4)]:
            prod.create_rate_card(business=b, user=owner, operation=op[key], rate=D(rate), effective_from=base, reason="Initial rate setup")
        prod.create_rate_card(business=b, user=owner, operation=op["stitch"], rate=D("48"), effective_from=base, worker=w[1], reason="Senior tailor rate")
        prod.create_rate_card(business=b, user=owner, operation=op["stitch"], rate=D("60"), effective_from=base, product=trouser, reason="Trousers are harder")

        # opening stock + purchases
        inv.manual_movement(business=b, user=acct, variant_id=mats["THR-001"].pk, movement_type="opening", quantity=10, unit_cost=D("55"), note="Opening stock")
        bill1 = pur.create_bill(business=b, user=acct, supplier_id=sup1.pk, supplier_bill_number="MT/2026/118", bill_date=d(5), lines=[
            {"variant_id": mats["FAB-SHIRT"].pk, "quantity": 400, "unit_cost": D("180"), "tax_amount": D("3428.57")},
            {"variant_id": mats["FAB-TROUSER"].pk, "quantity": 100, "unit_cost": D("260"), "tax_amount": D("1238.10")}])
        pur.post_bill(business=b, user=acct, bill_id=bill1.pk)
        bill2 = pur.create_bill(business=b, user=acct, supplier_id=sup2.pk, supplier_bill_number="ST-4471", bill_date=d(4), lines=[
            {"variant_id": mats["BTN-001"].pk, "quantity": 5000, "unit_cost": D("1.2"), "tax_amount": D("285.71")},
            {"variant_id": mats["ZIP-001"].pk, "quantity": 200, "unit_cost": D("9"), "tax_amount": D("85.71")},
            {"variant_id": mats["THR-001"].pk, "quantity": 30, "unit_cost": D("60"), "tax_amount": D("85.71")}])
        pur.post_bill(business=b, user=acct, bill_id=bill2.pk)

        # batch 1: shirts, full journey
        batch = prod.create_batch(business=b, user=prodn, product=shirt, start_date=d(6), expected_completion=today + timedelta(days=3),
                                  plans=[(sv["28"], 40, "Poly-cotton", 180), (sv["30"], 60, "Poly-cotton", 180), (sv["32"], 50, "Poly-cotton", 180)],
                                  notes="Green Valley - June order", client=cust[1])
        prod.issue_material(business=b, user=prodn, batch_id=batch.pk, variant_id=mats["FAB-SHIRT"].pk, quantity=D("300"))
        prod.issue_material(business=b, user=prodn, batch_id=batch.pk, variant_id=mats["BTN-001"].pk, quantity=D("1500"))
        stg = {s.stage.code: s for s in batch.stages.select_related("stage")}
        E = lambda **kw: prod.record_work_entry(business=b, user=prodn, batch_id=batch.pk, **kw)
        for size, q in (("28", 40), ("30", 60), ("32", 50)):
            E(worker_id=w[0].pk, operation_id=op["cut"].pk, work_date=d(5), variant_id=sv[size].pk, batch_stage_id=stg["CUT"].pk, good=q)
        E(worker_id=w[1].pk, operation_id=op["stitch"].pk, work_date=d(4), variant_id=sv["28"].pk, batch_stage_id=stg["STITCH"].pk, good=40)
        s30 = E(worker_id=w[1].pk, operation_id=op["stitch"].pk, work_date=d(4), variant_id=sv["30"].pk, batch_stage_id=stg["STITCH"].pk,
                good=54, rejected_worker=2, rework_worker=4, reason="stitching_defect")
        E(worker_id=w[2].pk, operation_id=op["stitch"].pk, work_date=d(3), variant_id=sv["32"].pk, batch_stage_id=stg["STITCH"].pk, good=50)
        E(worker_id=w[1].pk, operation_id=op["rework"].pk, work_date=d(3), variant_id=sv["30"].pk, batch_stage_id=stg["STITCH"].pk,
          good=3, rejected_other=1, reason="fabric_defect", rework_links=[(s30.pk, 4)])
        for size, q in (("28", 40), ("30", 57), ("32", 50)):
            E(worker_id=w[2].pk, operation_id=op["iron"].pk, work_date=d(2), variant_id=sv[size].pk, batch_stage_id=stg["IRON"].pk, good=q)
        E(worker_id=w[3].pk, operation_id=op["button"].pk, work_date=d(2), variant_id=sv["30"].pk, batch_stage_id=stg["STITCH"].pk, quantity=150)
        prod.record_work_entry(business=b, user=prodn, worker_id=w[3].pk, operation_id=op["button"].pk, work_date=d(1), quantity=D("80"), note="Manual: extra button work")
        for size, g, rw, ro, reason in (("28", 39, 0, 1, "fabric_defect"), ("30", 57, 0, 0, ""), ("32", 49, 1, 0, "stitching_defect")):
            prod.record_inspection(business=b, user=prodn, batch_stage_id=stg["QC"].pk, variant_id=sv[size].pk, inspected_date=d(1), good=g,
                                   rejected_worker=rw, rejected_other=ro, reason=reason)
            prod.receive_finished_goods(business=b, user=prodn, batch_id=batch.pk, variant_id=sv[size].pk, quantity=g)
        prod.complete_batch(business=b, user=prodn, batch_id=batch.pk)

        # batch 2: trousers with over-cutting, still in progress
        # client-specific route: this school's order skips ironing (the checklist is per batch)
        batch2 = prod.create_batch(business=b, user=prodn, product=trouser, start_date=d(1), expected_completion=today - timedelta(days=1),
                                   plans=[(tv["30"], 20, "Cotton twill", 220), (tv["32"], 20, "Cotton twill", 220)],
                                   notes="Little Angels - delayed", client=cust[2],
                                   stages=[st["CUT"], st["STITCH"], st["QC"], st["FG"]])
        s2 = {s.stage.code: s for s in batch2.stages.select_related("stage")}
        prod.issue_material(business=b, user=prodn, batch_id=batch2.pk, variant_id=mats["FAB-TROUSER"].pk, quantity=D("45"))
        prod.record_work_entry(business=b, user=prodn, batch_id=batch2.pk, worker_id=w[0].pk, operation_id=op["cut"].pk, work_date=today, variant_id=tv["30"].pk,
                               batch_stage_id=s2["CUT"].pk, good=22, over_cut_reason="Extra pieces cut to cover fabric flaws",
                               extra_materials=[(mats["FAB-TROUSER"].pk, D("3"))])

        # payroll
        pay.issue_advance(business=b, user=acct, worker_id=w[2].pk, amount=D("1000"), account_id=cash.pk)
        stm = {}
        for i in (0, 1, 2, 3):
            stm[i] = pay.create_statement(business=b, user=acct, worker_id=w[i].pk, period_start=d(6), period_end=today)
        sout = StageOutcome.objects.get(work_entry=s30)
        adj = pay.propose_adjustment(business=b, user=acct, statement_id=stm[1].pk, kind="deduction", amount=D("50"),
                                     reason="Repeated stitching defect on size 30", rejection_outcome_id=sout.pk)
        pay.decide_adjustment(business=b, user=owner, adjustment_id=adj.pk, approve=True, note="Approved")
        bonus = pay.propose_adjustment(business=b, user=acct, statement_id=stm[0].pk, kind="earning", amount=D("200"), reason="Overtime on cutting")
        pay.decide_adjustment(business=b, user=owner, adjustment_id=bonus.pk, approve=True)
        fin_st = {0: pay.finalize_statement(business=b, user=acct, statement_id=stm[0].pk),
                  1: pay.finalize_statement(business=b, user=acct, statement_id=stm[1].pk),
                  2: pay.finalize_statement(business=b, user=acct, statement_id=stm[2].pk, advance_recovery=D("500")),
                  3: pay.finalize_statement(business=b, user=acct, statement_id=stm[3].pk)}
        pay.pay_statement(business=b, user=acct, statement_id=fin_st[0].pk, amount=fin_st[0].net_payable, account_id=cash.pk)
        pay.pay_statement(business=b, user=acct, statement_id=fin_st[1].pk, amount=D("500"), account_id=cash.pk)   # partial

        # sales
        ch = sal.create_challan(business=b, user=acct, customer_id=cust[1].pk, challan_date=today, vehicle_no="MH05 AB 1234",
                                lines=[{"variant_id": sv["28"].pk, "quantity": 30}, {"variant_id": sv["30"].pk, "quantity": 40}])
        ch = sal.post_challan(business=b, user=acct, challan_id=ch.pk)
        cl = list(ch.lines.order_by("id"))
        inv1 = sal.create_invoice(business=b, user=acct, customer_id=cust[1].pk, invoice_date=today, lines=[
            {"variant_id": sv["28"].pk, "quantity": 30, "rate": D("450.50"), "challan_line_id": cl[0].pk},
            {"variant_id": sv["30"].pk, "quantity": 25, "rate": D("470"), "challan_line_id": cl[1].pk},
            {"variant_id": sv["32"].pk, "quantity": 10, "rate": D("490")}])    # direct line dispatches now
        inv1 = sal.post_invoice(business=b, user=acct, invoice_id=inv1.pk)
        inv2 = sal.create_invoice(business=b, user=acct, customer_id=cust[1].pk, invoice_date=today, lines=[
            {"variant_id": sv["30"].pk, "quantity": 15, "rate": D("470"), "challan_line_id": cl[1].pk}])
        sal.post_invoice(business=b, user=acct, invoice_id=inv2.pk)
        fin.create_receipt(business=b, user=acct, customer_id=cust[1].pk, account_id=bank.pk, amount=D("20000"), receipt_date=today, mode="bank",
                           reference="NEFT-88123", allocations=[{"doc_id": inv1.pk, "amount": D("20000")}])
        fin.create_supplier_payment(business=b, user=acct, supplier_id=sup1.pk, account_id=bank.pk, amount=D("50000"), payment_date=today,
                                    reference="NEFT-55012", allocations=[{"doc_id": bill1.pk, "amount": D("50000")}])
        for cat_, amt, desc in (("rent", 25000, "Factory rent"), ("electricity", 6400, "MSEB bill"), ("tea_snacks", 850, "Staff tea")):
            fin.create_expense(business=b, user=acct, category=cat_, amount=D(amt), account_id=cash.pk if cat_ == "tea_snacks" else bank.pk, expense_date=today, description=desc)

        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(f"Logins (development only): owner / accounts / production — password: {o['password']}")
