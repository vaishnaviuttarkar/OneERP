from django import forms
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from accounts.permissions import ACCOUNTS, OWNER, PRODUCTION, role_required
from core.forms import BootstrapMixin
from core.services import get_current_business
from core.viewkit import badge, form_page, handle_post, link, render_list, row_actions, search_filter
from production.models import Workflow
from . import services
from .models import ProductTemplate, ProductVariant


class ProductForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=200)
    kind = forms.ChoiceField(choices=ProductTemplate.KINDS)
    description = forms.CharField(required=False)
    default_workflow = forms.ModelChoiceField(queryset=Workflow.objects.none(), required=False,
                                              help_text="Required for finished goods.")

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["default_workflow"].queryset = Workflow.objects.filter(business=business, is_active=True)


class VariantForm(BootstrapMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=ProductTemplate.objects.none())
    sku = forms.CharField(max_length=50)
    size = forms.CharField(required=False)
    colour = forms.CharField(required=False)
    unit = forms.ChoiceField(choices=ProductVariant.UNITS)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["product"].queryset = ProductTemplate.objects.filter(business=business, is_active=True)


@role_required(OWNER, ACCOUNTS, PRODUCTION)
def products(request):
    b = get_current_business()
    qs = search_filter(ProductTemplate.objects.filter(business=b).order_by("name"), request.GET.get("q", "").strip(), "name")
    kind = request.GET.get("kind")
    if kind:
        qs = qs.filter(kind=kind)
    can_edit = request.user.effective_role in (OWNER, ACCOUNTS)
    rows = []
    for p in qs.prefetch_related("variants"):
        acts = row_actions(request, edit=f"/catalogue/products/{p.pk}/edit/" if can_edit else None,
                           delete=f"/catalogue/products/{p.pk}/delete/" if can_edit else None,
                           confirm=f"Delete product {p.name}? Products in use cannot be deleted.")
        rows.append([p.name, p.get_kind_display(), ", ".join(v.sku for v in p.variants.all()[:8]) or "-",
                     badge("Active" if p.is_active else "Inactive"), acts])
    return render_list(request, title="Products", headers=["Product", "Kind", "Variants (SKU)", "Status", ""], rows=rows,
                       add_url="/catalogue/products/new/" if can_edit else None, add_label="New product",
                       extra_actions=[("Add variant", "/catalogue/variants/new/")] if can_edit else [],
                       filters=[{"name": "kind", "label": "Kind", "current": kind or "", "options": ProductTemplate.KINDS}])


@role_required(OWNER, ACCOUNTS)
def product_create(request):
    b = get_current_business()
    form = ProductForm(request.POST or None, business=b)
    return form_page(request, form, "New product",
                     lambda f, fs: services.create_product(business=b, user=request.user, **f.cleaned_data),
                     "/catalogue/products/", back="/catalogue/products/",
                     note="After creating a product, add its variants (sizes/colours or the single raw-material variant).")


@role_required(OWNER, ACCOUNTS)
def product_edit(request, pk):
    b = get_current_business()
    p = get_object_or_404(ProductTemplate, pk=pk, business=b)
    initial = {"name": p.name, "kind": p.kind, "description": p.description, "default_workflow": None}
    pw = getattr(p, "default_workflow", None)
    if pw is not None:
        initial["default_workflow"] = pw.workflow_id
    form = ProductForm(request.POST or None, business=b, initial=initial)
    form.fields["kind"].disabled = True            # a product's kind is fixed once created
    form.fields["is_active"] = forms.BooleanField(required=False, initial=p.is_active, label="Active")
    if p.kind != ProductTemplate.FINISHED:
        form.fields["default_workflow"].disabled = True
    return form_page(request, form, f"Edit {p.name}",
                     lambda f, fs: services.update_product(business=b, user=request.user, product_id=p.pk, name=f.cleaned_data["name"],
                                                           description=f.cleaned_data["description"], default_workflow=f.cleaned_data["default_workflow"],
                                                           is_active=f.cleaned_data["is_active"]),
                     "/catalogue/products/", back="/catalogue/products/", submit="Save changes", success_msg="Product updated.",
                     note="Kind cannot change after creation. Deactivate a product instead of deleting it once it has been used.")


@role_required(OWNER, ACCOUNTS)
@require_POST
def product_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_product(business=b, user=request.user, product_id=pk), "/catalogue/products/", "Product deleted.")


@role_required(OWNER, ACCOUNTS)
def variant_edit(request, pk):
    b = get_current_business()
    v = get_object_or_404(ProductVariant.objects.select_related("product"), pk=pk, business=b)
    form = VariantForm(request.POST or None, business=b, initial={"product": v.product_id, "sku": v.sku, "size": v.size,
                                                                  "colour": v.colour, "unit": v.unit})
    form.fields["product"].disabled = True
    return form_page(request, form, f"Edit {v.sku}",
                     lambda f, fs: services.update_variant(business=b, user=request.user, variant_id=v.pk, sku=f.cleaned_data["sku"],
                                                           size=f.cleaned_data["size"], colour=f.cleaned_data["colour"], unit=f.cleaned_data["unit"]),
                     "/catalogue/products/", back="/catalogue/products/", submit="Save changes", success_msg="Variant updated.")


@role_required(OWNER, ACCOUNTS)
@require_POST
def variant_delete(request, pk):
    b = get_current_business()
    return handle_post(request, lambda: services.delete_variant(business=b, user=request.user, variant_id=pk), "/catalogue/products/", "Variant deleted.")


@role_required(OWNER, ACCOUNTS)
def variant_create(request):
    b = get_current_business()
    form = VariantForm(request.POST or None, business=b)
    return form_page(request, form, "New variant",
                     lambda f, fs: services.add_variant(business=b, user=request.user, **f.cleaned_data),
                     "/catalogue/products/", back="/catalogue/products/")
