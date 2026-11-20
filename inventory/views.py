from django import forms

from accounts.permissions import ACCOUNTS, OWNER, PRODUCTION, role_required
from catalogue.models import ProductVariant
from core.forms import BootstrapMixin
from core.services import get_current_business
from core.viewkit import form_page, inr, link, qf, render_list, search_filter
from . import selectors, services
from .models import StockMovement


class AdjustForm(BootstrapMixin, forms.Form):
    variant = forms.ModelChoiceField(queryset=ProductVariant.objects.none())
    movement_type = forms.ChoiceField(choices=[(StockMovement.OPENING, "Opening stock"),
                                               (StockMovement.ADJUSTMENT, "Adjustment (+/-)"),
                                               (StockMovement.DAMAGE, "Damage / rejection write-off")])
    quantity = forms.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = forms.DecimalField(max_digits=14, decimal_places=2, required=False,
                                   help_text="Needed for stock coming in (raw materials/accessories).")
    note = forms.CharField(max_length=200)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["variant"].queryset = ProductVariant.objects.filter(business=business, is_active=True).select_related("product")


@role_required(OWNER, ACCOUNTS, PRODUCTION)
def stock(request):
    b = get_current_business()
    show_value = request.user.effective_role in (OWNER, ACCOUNTS)   # production never sees financial values
    kind = request.GET.get("kind")
    rows = []
    for s in selectors.stock_rows(b, kind=kind, search=request.GET.get("q", "").strip()):
        row = [s.variant.label, s.variant.product.get_kind_display(), f"{qf(s.quantity)} {s.variant.unit}"]
        if show_value:
            avg = (s.value / s.quantity) if s.quantity and s.variant.product.kind != "finished" else None
            row += [inr(s.value), inr(avg) if avg is not None else "-"]
        rows.append(row)
    headers = ["Item", "Kind", "On hand"] + (["Stock value", "Avg cost"] if show_value else [])
    return render_list(request, title="Stock", headers=headers, rows=rows,
                       filters=[{"name": "kind", "label": "Kind", "current": kind or "",
                                 "options": [("finished", "Finished goods"), ("raw", "Raw materials"), ("accessory", "Accessories")]}],
                       add_url="/inventory/adjust/" if show_value else None, add_label="Stock adjustment",
                       extra_actions=[("Movements", "/inventory/movements/")] if show_value else [])


@role_required(OWNER, ACCOUNTS)
def movements(request):
    b = get_current_business()
    qs = StockMovement.objects.filter(business=b).select_related("variant__product")
    qs = search_filter(qs, request.GET.get("q", "").strip(), "variant__sku", "variant__product__name", "note")
    t = request.GET.get("type")
    if t:
        qs = qs.filter(movement_type=t)
    rows = [[m.created_at.strftime("%d %b %Y %H:%M"), m.variant.label, m.get_movement_type_display(), qf(m.quantity),
             inr(m.value), m.note] for m in qs[:500]]
    return render_list(request, title="Stock movement history", headers=["When", "Item", "Type", "Qty", "Value", "Reference"], rows=rows,
                       filters=[{"name": "type", "label": "Type", "current": t or "", "options": StockMovement.TYPES}],
                       subtitle="Immutable ledger (latest 500)")


@role_required(OWNER, ACCOUNTS)
def adjust(request):
    b = get_current_business()
    form = AdjustForm(request.POST or None, business=b)

    def handler(f, fs):
        d = f.cleaned_data
        return services.manual_movement(business=b, user=request.user, variant_id=d["variant"].pk,
                                        movement_type=d["movement_type"], quantity=d["quantity"],
                                        unit_cost=d["unit_cost"], note=d["note"])
    return form_page(request, form, "Stock adjustment", handler, "/inventory/movements/", back="/inventory/",
                     note="Stock can never go negative and cannot be backdated. Every change creates an immutable movement.")
