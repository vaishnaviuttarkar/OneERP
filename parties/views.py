from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from accounts.permissions import ACCOUNTS, OWNER, role_required
from core.forms import BootstrapMixin
from core.services import get_current_business
from core.viewkit import (detail_page, form_page, handle_post, inr, link, post_btn_csrf, render_list, search_filter)
from purchasing.selectors import bills_with_outstanding
from sales.selectors import invoices_with_outstanding
from . import services
from .models import Party


class PartyForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=200)
    is_customer = forms.BooleanField(required=False, label="Customer")
    is_supplier = forms.BooleanField(required=False, label="Supplier")
    contact_person = forms.CharField(required=False)
    phone = forms.CharField(required=False)
    email = forms.EmailField(required=False)
    address = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    tax_id = forms.CharField(required=False, label="Tax ID (optional)")


@role_required(OWNER, ACCOUNTS)
def party_list(request):
    b = get_current_business()
    qs = Party.objects.filter(business=b).order_by("name")
    qs = search_filter(qs, request.GET.get("q", "").strip(), "name", "phone", "email")
    role = request.GET.get("role")
    if role == "customer":
        qs = qs.filter(is_customer=True)
    elif role == "supplier":
        qs = qs.filter(is_supplier=True)
    elif role == "other":
        qs = qs.filter(is_customer=False, is_supplier=False)
    rows = []
    for p in qs:
        rows.append([link(f"/parties/{p.pk}/", p.name), p.roles_label, p.phone or "-", "Active" if p.is_active else "Inactive"])
    return render_list(request, title="Clientele", headers=["Name", "Role", "Phone", "Status"], rows=rows,
                       add_url="/parties/new/", add_label="New party",
                       filters=[{"name": "role", "label": "Role", "current": role or "",
                                 "options": [("customer", "Customers"), ("supplier", "Suppliers"), ("other", "Other")]}])


@role_required(OWNER, ACCOUNTS)
def party_create(request):
    b = get_current_business()
    form = PartyForm(request.POST or None)
    return form_page(request, form, "New party", lambda f, fs: services.create_party(business=b, user=request.user, **f.cleaned_data),
                     lambda p: f"/parties/{p.pk}/", back="/parties/",
                     note="A party with neither role ticked is an 'Other' party and cannot be used in stock or financial documents.")


@role_required(OWNER, ACCOUNTS)
def party_detail(request, pk):
    b = get_current_business()
    p = get_object_or_404(Party, pk=pk, business=b)
    sections = []
    if p.is_customer:
        invs = invoices_with_outstanding(b, customer=p)
        sections.append({"title": "Customer ledger - invoices", "headers": ["Invoice", "Date", "Total", "Received", "Outstanding"],
                         "rows": [[link(f"/sales/invoices/{i.pk}/", i.number), i.invoice_date, inr(i.total), inr(i.received), inr(i.outstanding)] for i in invs]})
        sections.append({"title": "Receipts", "headers": ["Receipt", "Date", "Amount"],
                         "rows": [[r.number, r.receipt_date, inr(r.amount)] for r in p.receipts.all()]})
    if p.is_supplier:
        bills = bills_with_outstanding(b, supplier=p)
        sections.append({"title": "Supplier ledger - bills", "headers": ["Bill", "Supplier bill #", "Date", "Total", "Paid", "Outstanding"],
                         "rows": [[link(f"/purchasing/{x.pk}/", x.number), x.supplier_bill_number, x.bill_date, inr(x.total), inr(x.paid), inr(x.outstanding)] for x in bills]})
        sections.append({"title": "Payments", "headers": ["Payment", "Date", "Amount"],
                         "rows": [[s.number, s.payment_date, inr(s.amount)] for s in p.payments.all()]})
    btn = post_btn_csrf(request, f"/parties/{p.pk}/toggle/", "Deactivate" if p.is_active else "Activate",
                        "Change this party's status?", "btn-outline-secondary")
    return detail_page(request, title=p.name, facts=[("Role", p.roles_label), ("Phone", p.phone or "-"), ("Email", p.email or "-"),
                       ("Status", "Active" if p.is_active else "Inactive")], sections=sections, buttons=[btn], back="/parties/")


@role_required(OWNER, ACCOUNTS)
def party_edit(request, pk):
    b = get_current_business()
    p = get_object_or_404(Party, pk=pk, business=b)
    initial = {"name": p.name, "is_customer": p.is_customer, "is_supplier": p.is_supplier, "contact_person": p.contact_person,
               "phone": p.phone, "email": p.email, "address": p.address, "tax_id": p.tax_id}
    form = PartyForm(request.POST or None, initial=initial)
    return form_page(request, form, f"Edit {p.name}",
                     lambda f, fs: services.update_party(business=b, user=request.user, party_id=p.pk, **f.cleaned_data),
                     lambda x: f"/parties/{x.pk}/", back=f"/parties/{p.pk}/", submit="Save changes", success_msg="Party updated.",
                     note="Roles that already have invoices, bills or batches cannot be removed.")


@role_required(OWNER, ACCOUNTS)
@require_POST
def party_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_party(business=b, user=request.user, party_id=pk), "/parties/", "Party deleted.")


@role_required(OWNER, ACCOUNTS)
@require_POST
def party_toggle(request, pk):
    b = get_current_business()
    p = get_object_or_404(Party, pk=pk, business=b)
    return handle_post(request, lambda: services.set_party_active(business=b, user=request.user, party_id=p.pk, active=not p.is_active),
                       f"/parties/{p.pk}/", "Party status updated.")
