from django import forms
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import ACCOUNTS, OWNER, role_required
from catalogue.models import ProductVariant
from core.forms import BootstrapMixin, DateInput
from core.services import get_current_business
from core.viewkit import badge, detail_page, form_page, handle_post, inr, link, post_btn_csrf, qf, render_list, row_actions, search_filter
from parties.models import Party
from . import services
from .models import DeliveryChallan, DeliveryChallanLine, SalesInvoice

ROLES = (OWNER, ACCOUNTS)


class ChallanForm(BootstrapMixin, forms.Form):
    customer = forms.ModelChoiceField(queryset=Party.objects.none())
    challan_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    vehicle_no = forms.CharField(required=False)
    notes = forms.CharField(required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["customer"].queryset = Party.objects.filter(business=business, is_customer=True, is_active=True)


class ChallanLineForm(BootstrapMixin, forms.Form):
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none(), required=False)
    quantity = forms.IntegerField(min_value=1, required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, is_active=True, product__kind="finished").select_related("product")


class InvoiceForm(BootstrapMixin, forms.Form):
    customer = forms.ModelChoiceField(queryset=Party.objects.none())
    invoice_date = forms.DateField(widget=DateInput(), initial=timezone.localdate)
    notes = forms.CharField(required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["customer"].queryset = Party.objects.filter(business=business, is_customer=True, is_active=True)


class InvoiceLineForm(ChallanLineForm):
    rate = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, label="Rate (₹)")
    challan_line = forms.ModelChoiceField(queryset=DeliveryChallanLine.objects.none(), required=False, label="From challan (optional)")

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, business=business, **k)
        qs = DeliveryChallanLine.objects.filter(business=business, challan__status="posted").select_related("challan", "variant")
        self.fields["challan_line"].queryset = qs
        self.fields["challan_line"].label_from_instance = lambda c: f"{c.challan.number}: {c.variant.sku} ×{c.quantity}"


ChallanLines = formset_factory(ChallanLineForm, extra=6)
InvoiceLines = formset_factory(InvoiceLineForm, extra=6)


@role_required(*ROLES)
def challans(request):
    b = get_current_business()
    qs = search_filter(DeliveryChallan.objects.filter(business=b).select_related("customer"), request.GET.get("q", "").strip(), "number", "customer__name")
    st = request.GET.get("status")
    if st:
        qs = qs.filter(status=st)
    rows = []
    for c in qs.prefetch_related("lines"):
        acts = row_actions(request, edit=f"/sales/challans/{c.pk}/edit/" if c.status == DeliveryChallan.DRAFT else None,
                           delete=f"/sales/challans/{c.pk}/delete/" if c.status == DeliveryChallan.DRAFT else None,
                           confirm="Delete this draft challan?")
        rows.append([link(f"/sales/challans/{c.pk}/", c.number or f"Draft #{c.pk}"), c.customer.name, c.challan_date,
                     sum(l.quantity for l in c.lines.all()), badge(c.status), acts])
    return render_list(request, title="Delivery challans", headers=["Challan", "Customer", "Date", "Pieces", "Status", ""], rows=rows, add_url="/sales/challans/new/",
                       add_label="New challan", filters=[{"name": "status", "label": "Status", "current": st or "", "options": DeliveryChallan.STATUSES}])


@role_required(*ROLES)
def challan_new(request):
    b = get_current_business()
    form = ChallanForm(request.POST or None, business=b)
    fs = ChallanLines(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        lines = [{"variant_id": c["variant"].pk, "quantity": c["quantity"]} for c in formsets["Lines"].cleaned_data if c.get("variant") and c.get("quantity")]
        d = f.cleaned_data
        return services.create_challan(business=b, user=request.user, customer_id=d["customer"].pk, challan_date=d["challan_date"], lines=lines,
                                       vehicle_no=d["vehicle_no"], notes=d["notes"])
    return form_page(request, form, "New delivery challan", handler, lambda c: f"/sales/challans/{c.pk}/", formsets={"Lines": fs}, back="/sales/challans/",
                     note="Posting a challan dispatches the stock exactly once.")


@role_required(*ROLES)
def challan_detail(request, pk):
    b = get_current_business()
    c = get_object_or_404(DeliveryChallan.objects.select_related("customer"), pk=pk, business=b)
    lines = []
    for l in c.lines.select_related("variant__product"):
        lines.append([l.variant.label, l.quantity, services.active_invoiced_qty(l) if c.status == "posted" else "-"])
    buttons = []
    if c.status == "draft":
        buttons = [post_btn_csrf(request, f"/sales/challans/{c.pk}/post/", "Post & dispatch", "Post this challan and dispatch stock?", "btn-success"),
                   post_btn_csrf(request, f"/sales/challans/{c.pk}/delete/", "Delete draft", "Delete?", "btn-outline-danger")]
    elif c.status == "posted":
        buttons = [post_btn_csrf(request, f"/sales/challans/{c.pk}/cancel/", "Cancel challan", "Cancel and reverse the dispatch?", "btn-outline-danger")]
    return detail_page(request, title=c.number or f"Draft challan #{c.pk}", badge_html=badge(c.status),
                       facts=[("Customer", c.customer.name), ("Date", c.challan_date), ("Vehicle", c.vehicle_no or "-")],
                       sections=[{"title": "Lines", "headers": ["Item", "Dispatched", "Invoiced (active)"], "rows": lines}], buttons=buttons,
                       print_url=f"/sales/challans/{c.pk}/print/" if c.status != "draft" else None, back="/sales/challans/")


@role_required(*ROLES)
@require_POST
def challan_action(request, pk, action):
    b = get_current_business()
    fn = {"post": (services.post_challan, "Challan posted - stock dispatched."), "cancel": (services.cancel_challan, "Challan cancelled."),
          "delete": (services.delete_draft_challan, "Draft deleted.")}[action]
    return handle_post(request, lambda: fn[0](business=b, user=request.user, challan_id=pk), "/sales/challans/" if action == "delete" else f"/sales/challans/{pk}/", fn[1])


@role_required(*ROLES)
def challan_print(request, pk):
    b = get_current_business()
    c = get_object_or_404(DeliveryChallan.objects.select_related("customer"), pk=pk, business=b)
    return render(request, "print/challan.html", {"business": b, "c": c, "title": "Delivery Challan", "lines": c.lines.select_related("variant__product")})


@role_required(*ROLES)
def invoices(request):
    b = get_current_business()
    qs = search_filter(SalesInvoice.objects.filter(business=b).select_related("customer"), request.GET.get("q", "").strip(), "number", "customer__name")
    st = request.GET.get("status")
    if st:
        qs = qs.filter(status=st)
    rows = []
    for i in qs:
        acts = row_actions(request, edit=f"/sales/invoices/{i.pk}/edit/" if i.status == SalesInvoice.DRAFT else None,
                           delete=f"/sales/invoices/{i.pk}/delete/" if i.status == SalesInvoice.DRAFT else None,
                           confirm="Delete this draft invoice?")
        rows.append([link(f"/sales/invoices/{i.pk}/", i.number or f"Draft #{i.pk}"), i.customer.name, i.invoice_date, inr(i.total), badge(i.status), acts])
    return render_list(request, title="Sales invoices", headers=["Invoice", "Customer", "Date", "Total", "Status", ""], rows=rows, add_url="/sales/invoices/new/",
                       add_label="New invoice", filters=[{"name": "status", "label": "Status", "current": st or "", "options": SalesInvoice.STATUSES}])


@role_required(*ROLES)
def invoice_edit(request, pk):
    b = get_current_business()
    inv = get_object_or_404(SalesInvoice.objects.select_related("customer"), pk=pk, business=b)
    if inv.status != SalesInvoice.DRAFT:
        return redirect(f"/sales/invoices/{inv.pk}/")
    form = InvoiceForm(request.POST or None, business=b, initial={"customer": inv.customer_id, "invoice_date": inv.invoice_date, "notes": inv.notes})
    init = [{"variant": l.variant_id, "quantity": l.quantity, "rate": l.rate, "challan_line": l.challan_line_id}
            for l in inv.lines.order_by("id")]
    fs = InvoiceLines(request.POST or None, initial=init, form_kwargs={"business": b})

    def handler(f, formsets):
        lines = []
        for c in formsets["Lines"].cleaned_data:
            cl = c.get("challan_line")
            v = c.get("variant") or (cl.variant if cl else None)
            if v and c.get("quantity"):
                lines.append({"variant_id": v.pk, "quantity": c["quantity"], "rate": c.get("rate") or 0, "challan_line_id": cl.pk if cl else None})
        d = f.cleaned_data
        return services.update_draft_invoice(business=b, user=request.user, invoice_id=inv.pk, customer_id=d["customer"].pk,
                                             invoice_date=d["invoice_date"], lines=lines, notes=d["notes"])
    return form_page(request, form, f"Edit draft invoice #{inv.pk}", handler, lambda i: f"/sales/invoices/{i.pk}/", formsets={"Lines": fs},
                     back=f"/sales/invoices/{inv.pk}/", submit="Save draft", success_msg="Draft invoice updated.")


@role_required(*ROLES)
@require_POST
def invoice_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_draft_invoice(business=b, user=request.user, invoice_id=pk), "/sales/invoices/", "Draft deleted.")


@role_required(*ROLES)
def challan_edit(request, pk):
    b = get_current_business()
    ch = get_object_or_404(DeliveryChallan.objects.select_related("customer"), pk=pk, business=b)
    if ch.status != DeliveryChallan.DRAFT:
        return redirect(f"/sales/challans/{ch.pk}/")
    form = ChallanForm(request.POST or None, business=b, initial={"customer": ch.customer_id, "challan_date": ch.challan_date,
                                                                  "vehicle_no": ch.vehicle_no, "notes": ch.notes})
    init = [{"variant": l.variant_id, "quantity": l.quantity} for l in ch.lines.order_by("id")]
    fs = ChallanLines(request.POST or None, initial=init, form_kwargs={"business": b})

    def handler(f, formsets):
        lines = [{"variant_id": c["variant"].pk, "quantity": c["quantity"]} for c in formsets["Lines"].cleaned_data if c.get("variant") and c.get("quantity")]
        d = f.cleaned_data
        return services.update_draft_challan(business=b, user=request.user, challan_id=ch.pk, customer_id=d["customer"].pk, challan_date=d["challan_date"],
                                             lines=lines, vehicle_no=d["vehicle_no"], notes=d["notes"])
    return form_page(request, form, f"Edit draft challan #{ch.pk}", handler, lambda c: f"/sales/challans/{c.pk}/", formsets={"Lines": fs},
                     back=f"/sales/challans/{ch.pk}/", submit="Save draft", success_msg="Draft challan updated.")


@role_required(*ROLES)
@require_POST
def challan_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_draft_challan(business=b, user=request.user, challan_id=pk), "/sales/challans/", "Draft deleted.")


@role_required(*ROLES)
def invoice_new(request):
    b = get_current_business()
    form = InvoiceForm(request.POST or None, business=b)
    fs = InvoiceLines(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        lines = []
        for c in formsets["Lines"].cleaned_data:
            cl = c.get("challan_line")
            v = c.get("variant") or (cl.variant if cl else None)
            if v and c.get("quantity"):
                lines.append({"variant_id": v.pk, "quantity": c["quantity"], "rate": c.get("rate") or 0, "challan_line_id": cl.pk if cl else None})
        d = f.cleaned_data
        return services.create_invoice(business=b, user=request.user, customer_id=d["customer"].pk, invoice_date=d["invoice_date"], lines=lines, notes=d["notes"])
    return form_page(request, form, "New sales invoice", handler, lambda i: f"/sales/invoices/{i.pk}/", formsets={"Lines": fs}, back="/sales/invoices/",
                     note="Lines picked 'From challan' do not dispatch again. Lines without a challan dispatch stock when the invoice is posted. Total is rounded to the nearest rupee on posting.")


@role_required(*ROLES)
def invoice_detail(request, pk):
    b = get_current_business()
    i = get_object_or_404(SalesInvoice.objects.select_related("customer"), pk=pk, business=b)
    lines = [[l.variant.label, l.quantity, inr(l.rate), inr(l.amount), l.challan_line.challan.number if l.challan_line_id else "Direct"]
             for l in i.lines.select_related("variant__product", "challan_line__challan")]
    received = sum((a.amount for a in i.receipt_allocations.all()), 0)
    buttons = []
    if i.status == "draft":
        buttons = [post_btn_csrf(request, f"/sales/invoices/{i.pk}/post/", "Post invoice", "Post this invoice? It becomes immutable.", "btn-success"),
                   post_btn_csrf(request, f"/sales/invoices/{i.pk}/delete/", "Delete draft", "Delete?", "btn-outline-danger")]
    elif i.status == "posted":
        buttons = [post_btn_csrf(request, f"/sales/invoices/{i.pk}/cancel/", "Cancel invoice", "Cancel this invoice?", "btn-outline-danger")]
    return detail_page(request, title=i.number or f"Draft invoice #{i.pk}", badge_html=badge(i.status),
                       facts=[("Customer", i.customer.name), ("Date", i.invoice_date), ("Subtotal", inr(i.subtotal)), ("Round off", inr(i.round_off)),
                              ("Total", inr(i.total)), ("Received / outstanding", f"{inr(received)} / {inr(i.total - received) if i.status == 'posted' else '-'}")],
                       sections=[{"title": "Lines", "headers": ["Item", "Qty", "Rate", "Amount", "Source"], "rows": lines}], buttons=buttons,
                       print_url=f"/sales/invoices/{i.pk}/print/" if i.status != "draft" else None, back="/sales/invoices/")


@role_required(*ROLES)
@require_POST
def invoice_action(request, pk, action):
    b = get_current_business()
    fn = {"post": (services.post_invoice, "Invoice posted."), "cancel": (services.cancel_invoice, "Invoice cancelled."),
          "delete": (services.delete_draft_invoice, "Draft deleted.")}[action]
    return handle_post(request, lambda: fn[0](business=b, user=request.user, invoice_id=pk), "/sales/invoices/" if action == "delete" else f"/sales/invoices/{pk}/", fn[1])


@role_required(*ROLES)
def invoice_print(request, pk):
    b = get_current_business()
    i = get_object_or_404(SalesInvoice.objects.select_related("customer"), pk=pk, business=b)
    return render(request, "print/invoice.html", {"business": b, "i": i, "title": "Tax Invoice" if False else "Invoice", "lines": i.lines.select_related("variant__product")})
