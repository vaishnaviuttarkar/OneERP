from django import forms
from django.contrib import messages
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from accounts.permissions import ACCOUNTS, OWNER, role_required
from catalogue.models import ProductVariant
from core.forms import BootstrapMixin, DateInput
from core.services import get_current_business
from core.utils import PAYMENT_STATUSES, payment_status
from core.viewkit import badge, detail_page, form_page, handle_post, inr, link, payment_badge, post_btn_csrf, qf, render_list, row_actions, search_filter
from django.utils import timezone
from parties.models import Party
from . import services
from .models import PurchaseBill


class BillForm(BootstrapMixin, forms.Form):
    supplier = forms.ModelChoiceField(queryset=Party.objects.none())
    supplier_bill_number = forms.CharField(max_length=50)
    bill_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["supplier"].queryset = Party.objects.filter(business=business, is_supplier=True, is_active=True)


class LineForm(BootstrapMixin, forms.Form):
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False)
    quantity = forms.DecimalField(max_digits=14, decimal_places=3, required=False, min_value=0)
    unit_cost = forms.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0, label="Unit cost (incl. tax)")
    tax_amount = forms.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0, label="Recoverable tax in line")

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["variant"].queryset = ProductVariant.objects.filter(
            business=business, is_active=True, product__kind__in=["raw", "accessory"]).select_related("product")


LineFormSet = formset_factory(LineForm, extra=5)


def _bill_paid_map(business):
    """bill pk -> amount paid (allocations from supplier payments)."""
    from django.db.models import Sum
    from finance.models import SupplierPaymentAllocation
    return {r["bill_id"]: r["t"] for r in SupplierPaymentAllocation.objects.filter(business=business).order_by()
            .values("bill_id").annotate(t=Sum("amount"))}


@role_required(OWNER, ACCOUNTS)
def bill_list(request):
    b = get_current_business()
    qs = search_filter(PurchaseBill.objects.filter(business=b).select_related("supplier"), request.GET.get("q", "").strip(),
                       "number", "supplier_bill_number", "supplier__name")
    st, pay = request.GET.get("status", ""), request.GET.get("payment", "")
    if st:
        qs = qs.filter(status=st)
    paid = _bill_paid_map(b)
    rows = []
    for x in qs:
        state = payment_status(x.total, paid.get(x.pk, 0)) if x.status == PurchaseBill.POSTED else None
        if pay and state != pay:
            continue
        pay_cell = payment_badge(state) if state else badge("Draft")
        # posted bills are immutable (stock and ledger); drafts can be opened and deleted
        acts = row_actions(request, delete=f"/purchasing/{x.pk}/delete/" if x.status == PurchaseBill.DRAFT else None,
                           confirm=f"Delete draft bill {x.supplier_bill_number}?")
        rows.append([link(f"/purchasing/{x.pk}/", x.number or f"Draft #{x.pk}"), x.supplier.name, x.supplier_bill_number, x.bill_date,
                     inr(x.total), badge(x.status), pay_cell, acts])
    return render_list(request, title="Purchase bills", headers=["No.", "Supplier", "Supplier bill #", "Date", "Total", "Status", "Payment", ""],
                       rows=rows, add_url="/purchasing/new/", add_label="New bill",
                       filters=[{"name": "status", "label": "Status", "current": st, "options": PurchaseBill.STATUSES},
                                {"name": "payment", "label": "Payment", "current": pay, "options": PAYMENT_STATUSES}])


@role_required(OWNER, ACCOUNTS)
def bill_create(request):
    b = get_current_business()
    form = BillForm(request.POST or None, business=b)
    fs = LineFormSet(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        lines = [{"variant_id": c["variant"].pk, "quantity": c["quantity"], "unit_cost": c.get("unit_cost") or 0,
                  "tax_amount": c.get("tax_amount") or 0}
                 for c in formsets["Lines"].cleaned_data if c.get("variant") and c.get("quantity")]
        return services.create_bill(business=b, user=request.user, supplier_id=f.cleaned_data["supplier"].pk,
                                    supplier_bill_number=f.cleaned_data["supplier_bill_number"],
                                    bill_date=f.cleaned_data["bill_date"], lines=lines)
    return form_page(request, form, "New purchase bill", handler, lambda x: f"/purchasing/{x.pk}/",
                     formsets={"Lines": fs}, back="/purchasing/",
                     note="Unit cost is tax-inclusive; the recoverable tax portion is excluded from stock cost.")


@role_required(OWNER, ACCOUNTS)
def bill_detail(request, pk):
    b = get_current_business()
    bill = get_object_or_404(PurchaseBill.objects.select_related("supplier"), pk=pk, business=b)
    lines = [[l.variant.label, qf(l.quantity), inr(l.unit_cost), inr(l.tax_amount), inr(l.line_amount),
              inr(l.line_amount - l.tax_amount)] for l in bill.lines.select_related("variant__product")]
    buttons = []
    if bill.status == "draft":
        buttons = [post_btn_csrf(request, f"/purchasing/{bill.pk}/post/", "Post bill", "Post this bill and receive stock? This cannot be undone.", "btn-success"),
                   post_btn_csrf(request, f"/purchasing/{bill.pk}/delete/", "Delete draft", "Delete this draft?", "btn-outline-danger")]
    paid = _bill_paid_map(b).get(bill.pk, 0)
    state = payment_status(bill.total, paid) if bill.status == PurchaseBill.POSTED else None
    badge_html = badge(bill.status) + (" " + str(payment_badge(state)) if state else "")
    return detail_page(request, title=bill.number or f"Draft purchase bill #{bill.pk}", badge_html=badge_html,
                       facts=[("Supplier", bill.supplier.name), ("Supplier bill #", bill.supplier_bill_number),
                              ("Bill date", bill.bill_date), ("Total", inr(bill.total)), ("Tax (recoverable)", inr(bill.tax_total)),
                              ("Paid / outstanding", f"{inr(paid)} / {inr(bill.total - paid)}" if state else "-")],
                       sections=[{"title": "Lines", "headers": ["Item", "Qty", "Unit cost", "Tax", "Line amount", "Stock cost"], "rows": lines}],
                       buttons=buttons, back="/purchasing/")


@role_required(OWNER, ACCOUNTS)
@require_POST
def bill_post(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.post_bill(business=b, user=request.user, bill_id=pk),
                       f"/purchasing/{pk}/", "Bill posted and stock received.")


@role_required(OWNER, ACCOUNTS)
@require_POST
def bill_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_draft_bill(business=b, user=request.user, bill_id=pk),
                       "/purchasing/", "Draft deleted.")
