from rest_framework import serializers as s
from rest_framework.response import Response

from accounts.permissions import ACCOUNTS, OWNER, PRODUCTION
from finance import services as fin
from inventory.selectors import stock_rows
from payroll import services as pay
from production import services as prod
from purchasing import services as pur
from sales import services as sal
from .api import BusinessAPIView

MONEY = (OWNER, ACCOUNTS)
PROD = (OWNER, ACCOUNTS, PRODUCTION)
DEC = dict(max_digits=14, decimal_places=3)
MON = dict(max_digits=14, decimal_places=2)


class Op(BusinessAPIView):
    """POST operation: validate with serializer, run the service, optional Idempotency-Key header."""
    serializer_class = None
    action = ""
    status_code = 201

    def run(self, request, data, **kw):
        raise NotImplementedError

    def post(self, request, **kw):
        ser = self.serializer_class(data=request.data) if self.serializer_class else None
        data = {}
        if ser is not None:
            ser.is_valid(raise_exception=True)
            data = ser.validated_data
        payload = {"data": request.data, "kw": kw}
        return self.execute(request, self.action, payload, lambda: self.run(request, data, **kw), self.status_code)


def brief(obj, **extra):
    d = {"id": obj.pk}
    for k in ("number", "status"):
        if hasattr(obj, k):
            d[k] = getattr(obj, k)
    d.update(extra)
    return d


# ---- serializers
class PBLine(s.Serializer):
    variant_id = s.IntegerField()
    quantity = s.DecimalField(**DEC)
    unit_cost = s.DecimalField(**MON)
    tax_amount = s.DecimalField(required=False, default=0, **MON)


class PBSer(s.Serializer):
    supplier_id = s.IntegerField()
    supplier_bill_number = s.CharField()
    bill_date = s.DateField()
    lines = PBLine(many=True)


class PlanLine(s.Serializer):
    variant_id = s.IntegerField()
    quantity = s.DecimalField(**DEC)


class BatchSer(s.Serializer):
    product_id = s.IntegerField()
    start_date = s.DateField()
    expected_completion = s.DateField(required=False, allow_null=True)
    plans = PlanLine(many=True)


class IssueSer(s.Serializer):
    variant_id = s.IntegerField()
    quantity = s.DecimalField(**DEC)


class FGSer(IssueSer):
    pass


class Link(s.Serializer):
    source_entry_id = s.IntegerField()
    quantity = s.DecimalField(**DEC)


class WorkEntrySer(s.Serializer):
    worker_id = s.IntegerField()
    operation_id = s.IntegerField()
    work_date = s.DateField()
    variant_id = s.IntegerField(required=False, allow_null=True)
    batch_stage_id = s.IntegerField(required=False, allow_null=True)
    quantity = s.DecimalField(required=False, allow_null=True, **DEC)
    good = s.DecimalField(required=False, default=0, **DEC)
    rejected_worker = s.DecimalField(required=False, default=0, **DEC)
    rejected_other = s.DecimalField(required=False, default=0, **DEC)
    rework_worker = s.DecimalField(required=False, default=0, **DEC)
    rework_other = s.DecimalField(required=False, default=0, **DEC)
    reason = s.CharField(required=False, allow_blank=True, default="")
    note = s.CharField(required=False, allow_blank=True, default="")
    rework_links = Link(many=True, required=False)
    over_cut_reason = s.CharField(required=False, allow_blank=True, default="")
    extra_materials = IssueSer(many=True, required=False)


class StatementSer(s.Serializer):
    worker_id = s.IntegerField()
    period_start = s.DateField()
    period_end = s.DateField()


class FinalizeSer(s.Serializer):
    advance_recovery = s.DecimalField(required=False, default=0, **MON)


class PaySer(s.Serializer):
    amount = s.DecimalField(**MON)
    account_id = s.IntegerField()
    payment_date = s.DateField(required=False)


class DocLine(s.Serializer):
    variant_id = s.IntegerField()
    quantity = s.DecimalField(**DEC)
    rate = s.DecimalField(required=False, default=0, **MON)
    challan_line_id = s.IntegerField(required=False, allow_null=True)


class ChallanSer(s.Serializer):
    customer_id = s.IntegerField()
    challan_date = s.DateField()
    lines = DocLine(many=True)


class InvoiceSer(s.Serializer):
    customer_id = s.IntegerField()
    invoice_date = s.DateField()
    lines = DocLine(many=True)


class Alloc(s.Serializer):
    doc_id = s.IntegerField()
    amount = s.DecimalField(**MON)


class ReceiptSer(s.Serializer):
    customer_id = s.IntegerField()
    account_id = s.IntegerField()
    amount = s.DecimalField(**MON)
    receipt_date = s.DateField()
    mode = s.CharField(required=False, default="cash")
    reference = s.CharField(required=False, allow_blank=True, default="")
    allocations = Alloc(many=True, required=False)


class SupplierPaymentSer(s.Serializer):
    supplier_id = s.IntegerField()
    account_id = s.IntegerField()
    amount = s.DecimalField(**MON)
    payment_date = s.DateField()
    mode = s.CharField(required=False, default="bank")
    reference = s.CharField(required=False, allow_blank=True, default="")
    allocations = Alloc(many=True, required=False)


class ExpenseSer(s.Serializer):
    category = s.CharField()
    amount = s.DecimalField(**MON)
    account_id = s.IntegerField()
    expense_date = s.DateField()
    description = s.CharField(required=False, allow_blank=True, default="")


# ---- views
class StockView(BusinessAPIView):
    allowed_roles = PROD

    def get(self, request):
        show = request.user.effective_role in MONEY      # production never receives values
        out = []
        for r in stock_rows(self.business):
            row = {"variant_id": r.variant_id, "sku": r.variant.sku, "name": r.variant.label, "quantity": str(r.quantity)}
            if show:
                row["value"] = str(r.value)
            out.append(row)
        return Response(out)


class PurchaseBillCreate(Op):
    allowed_roles = MONEY; serializer_class = PBSer; action = "purchase_bill.create"
    def run(self, request, d):
        return brief(pur.create_bill(business=self.business, user=request.user, supplier_id=d["supplier_id"], supplier_bill_number=d["supplier_bill_number"],
                                     bill_date=d["bill_date"], lines=[dict(l) for l in d["lines"]]))


class PurchaseBillPost(Op):
    allowed_roles = MONEY; action = "purchase_bill.post"; status_code = 200
    def run(self, request, d, pk):
        return brief(pur.post_bill(business=self.business, user=request.user, bill_id=pk))


class BatchCreate(Op):
    allowed_roles = PROD; serializer_class = BatchSer; action = "batch.create"
    def run(self, request, d):
        from catalogue.models import ProductTemplate, ProductVariant
        from core.exceptions import DomainError
        product = ProductTemplate.objects.filter(pk=d["product_id"], business=self.business).first()
        if not product:
            raise DomainError("Product not found.")
        plans = [(ProductVariant.objects.get(pk=p["variant_id"], business=self.business), p["quantity"]) for p in d["plans"]]
        return brief(prod.create_batch(business=self.business, user=request.user, product=product, plans=plans, start_date=d["start_date"],
                                       expected_completion=d.get("expected_completion")))


class MaterialIssueView(Op):
    allowed_roles = PROD; serializer_class = IssueSer; action = "material.issue"
    def run(self, request, d, pk):
        i = prod.issue_material(business=self.business, user=request.user, batch_id=pk, variant_id=d["variant_id"], quantity=d["quantity"])
        r = {"id": i.pk, "quantity": str(i.quantity)}
        if request.user.effective_role in MONEY:
            r["value"] = str(i.value)
        return r


class FGReceiptView(Op):
    allowed_roles = PROD; serializer_class = FGSer; action = "fg.receive"
    def run(self, request, d, pk):
        r = prod.receive_finished_goods(business=self.business, user=request.user, batch_id=pk, variant_id=d["variant_id"], quantity=d["quantity"])
        return {"id": r.pk, "quantity": str(r.quantity)}


class WorkEntryView(Op):
    allowed_roles = PROD; serializer_class = WorkEntrySer; action = "work_entry.create"
    def run(self, request, d):
        batch_stage_id = d.get("batch_stage_id")
        e = prod.record_work_entry(
            business=self.business, user=request.user, worker_id=d["worker_id"], operation_id=d["operation_id"], work_date=d["work_date"],
            variant_id=d.get("variant_id"), batch_stage_id=batch_stage_id, quantity=d.get("quantity"), good=d["good"],
            rejected_worker=d["rejected_worker"], rejected_other=d["rejected_other"], rework_worker=d["rework_worker"], rework_other=d["rework_other"],
            reason=d["reason"], note=d["note"], rework_links=[(l["source_entry_id"], l["quantity"]) for l in d.get("rework_links", [])],
            over_cut_reason=d["over_cut_reason"], extra_materials=[(m["variant_id"], m["quantity"]) for m in d.get("extra_materials", [])])
        r = {"id": e.pk, "processed_qty": str(e.processed_qty), "paid_qty": str(e.paid_qty)}
        if request.user.effective_role in MONEY:     # production must never see rates / earnings
            r.update(rate=str(e.rate), rate_tier=e.rate_tier, earnings=str(e.earnings))
        return r


class StatementCreate(Op):
    allowed_roles = MONEY; serializer_class = StatementSer; action = "wage_statement.create"
    def run(self, request, d):
        st = pay.create_statement(business=self.business, user=request.user, worker_id=d["worker_id"], period_start=d["period_start"], period_end=d["period_end"])
        return brief(st, gross=str(st.gross_earnings))


class StatementFinalize(Op):
    allowed_roles = MONEY; serializer_class = FinalizeSer; action = "wage_statement.finalize"; status_code = 200
    def run(self, request, d, pk):
        st = pay.finalize_statement(business=self.business, user=request.user, statement_id=pk, advance_recovery=d["advance_recovery"])
        return brief(st, net_payable=str(st.net_payable))


class StatementPay(Op):
    allowed_roles = MONEY; serializer_class = PaySer; action = "wage_payment.create"
    def run(self, request, d, pk):
        p = pay.pay_statement(business=self.business, user=request.user, statement_id=pk, amount=d["amount"], account_id=d["account_id"], payment_date=d.get("payment_date"))
        return {"id": p.pk, "applied": str(p.applied_amount), "excess_to_advance": str(p.excess_amount)}


class ChallanCreate(Op):
    allowed_roles = MONEY; serializer_class = ChallanSer; action = "challan.create"
    def run(self, request, d):
        return brief(sal.create_challan(business=self.business, user=request.user, customer_id=d["customer_id"], challan_date=d["challan_date"],
                                        lines=[{"variant_id": l["variant_id"], "quantity": l["quantity"]} for l in d["lines"]]))


class ChallanAction(Op):
    allowed_roles = MONEY; status_code = 200
    which = "post"

    @property
    def action(self):
        return f"challan.{self.which}"
    def run(self, request, d, pk):
        fn = sal.post_challan if self.which == "post" else sal.cancel_challan
        r = fn(business=self.business, user=request.user, challan_id=pk)
        return brief(r) if r else {"id": pk, "status": "cancelled"}


class InvoiceCreate(Op):
    allowed_roles = MONEY; serializer_class = InvoiceSer; action = "invoice.create"
    def run(self, request, d):
        return brief(sal.create_invoice(business=self.business, user=request.user, customer_id=d["customer_id"], invoice_date=d["invoice_date"],
                                        lines=[dict(l) for l in d["lines"]]))


class InvoiceAction(Op):
    allowed_roles = MONEY; status_code = 200
    which = "post"

    @property
    def action(self):
        return f"invoice.{self.which}"
    def run(self, request, d, pk):
        fn = sal.post_invoice if self.which == "post" else sal.cancel_invoice
        r = fn(business=self.business, user=request.user, invoice_id=pk)
        return brief(r, total=str(r.total)) if r else {"id": pk, "status": "cancelled"}


class ReceiptCreate(Op):
    allowed_roles = MONEY; serializer_class = ReceiptSer; action = "receipt.create"
    def run(self, request, d):
        r = fin.create_receipt(business=self.business, user=request.user, customer_id=d["customer_id"], account_id=d["account_id"], amount=d["amount"],
                               receipt_date=d["receipt_date"], mode=d["mode"], reference=d["reference"], allocations=[dict(a) for a in d.get("allocations", [])])
        return {"id": r.pk, "number": r.number}


class SupplierPaymentCreate(Op):
    allowed_roles = MONEY; serializer_class = SupplierPaymentSer; action = "supplier_payment.create"
    def run(self, request, d):
        p = fin.create_supplier_payment(business=self.business, user=request.user, supplier_id=d["supplier_id"], account_id=d["account_id"], amount=d["amount"],
                                        payment_date=d["payment_date"], mode=d["mode"], reference=d["reference"], allocations=[dict(a) for a in d.get("allocations", [])])
        return {"id": p.pk, "number": p.number}


class ExpenseCreate(Op):
    allowed_roles = MONEY; serializer_class = ExpenseSer; action = "expense.create"
    def run(self, request, d):
        e = fin.create_expense(business=self.business, user=request.user, category=d["category"], amount=d["amount"], account_id=d["account_id"],
                               expense_date=d["expense_date"], description=d["description"])
        return {"id": e.pk, "number": e.number}


class ReconciliationView(BusinessAPIView):
    allowed_roles = ()   # Owner only

    def get(self, request):
        from reports.reconciliation import run_all
        return Response([{"group": c.group, "name": c.name, "ok": c.ok, "issues": c.issues} for c in run_all(self.business)])
