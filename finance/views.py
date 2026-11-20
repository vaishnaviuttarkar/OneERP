from django import forms
from django.forms import formset_factory
from django.utils import timezone

from accounts.permissions import ACCOUNTS, OWNER, role_required
from core.forms import BootstrapMixin, DateInput
from core.models import FinancialAccount
from core.services import get_current_business
from core.viewkit import form_page, inr, render_list, search_filter
from parties.models import Party
from purchasing.selectors import bills_with_outstanding
from sales.selectors import invoices_with_outstanding
from . import services
from .models import CashBookEntry, Expense, Receipt, SupplierPayment

ROLES = (OWNER, ACCOUNTS)


class _Base(BootstrapMixin, forms.Form):
    account = forms.ModelChoiceField(queryset=FinancialAccount.objects.none())
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    date = forms.DateField(widget=DateInput(), initial=timezone.localdate)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["account"].queryset = FinancialAccount.objects.filter(business=business, is_active=True)


class ReceiptForm(_Base):
    customer = forms.ModelChoiceField(queryset=Party.objects.none())
    mode = forms.ChoiceField(choices=Receipt.MODES)
    reference = forms.CharField(required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, business=business, **k)
        self.fields["customer"].queryset = Party.objects.filter(business=business, is_customer=True, is_active=True)


class SupplierPaymentForm(_Base):
    supplier = forms.ModelChoiceField(queryset=Party.objects.none())
    mode = forms.ChoiceField(choices=Receipt.MODES, initial="bank")
    reference = forms.CharField(required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, business=business, **k)
        self.fields["supplier"].queryset = Party.objects.filter(business=business, is_supplier=True, is_active=True)


class ExpenseForm(_Base):
    category = forms.ChoiceField(choices=Expense.CATEGORIES)
    description = forms.CharField(required=False)


class InvAllocForm(BootstrapMixin, forms.Form):
    invoice = forms.ChoiceField(required=False, choices=[])
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["invoice"].choices = [("", "-")] + [(i.pk, f"{i.number} · {i.customer.name} · due {i.outstanding}") for i in invoices_with_outstanding(business, only_open=True)]


class BillAllocForm(BootstrapMixin, forms.Form):
    invoice = forms.ChoiceField(required=False, choices=[], label="Bill")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False)

    def __init__(self, *a, business=None, **k):
        super().__init__(*a, **k)
        self.fields["invoice"].choices = [("", "-")] + [(x.pk, f"{x.number} · {x.supplier.name} · due {x.outstanding}") for x in bills_with_outstanding(business, only_open=True)]


InvAllocs = formset_factory(InvAllocForm, extra=4)
BillAllocs = formset_factory(BillAllocForm, extra=4)


def _allocs(fs):
    return [{"doc_id": int(c["invoice"]), "amount": c["amount"]} for c in fs.cleaned_data if c.get("invoice") and c.get("amount")]


@role_required(*ROLES)
def overview(request):
    """Finance overview: stock value, dues, wages, cash flow and where the money is."""
    from django.shortcuts import render
    from reports.selectors import finance_overview
    b = get_current_business()
    f = finance_overview(b)
    return render(request, "finance/overview.html", {"f": f, "chart": f["chart"], "business": b})


@role_required(*ROLES)
def receipts(request):
    b = get_current_business()
    qs = search_filter(Receipt.objects.filter(business=b).select_related("customer", "account"), request.GET.get("q", "").strip(), "number", "customer__name", "reference")
    rows = [[r.number, r.customer.name, r.receipt_date, r.account.name, r.get_mode_display(), inr(r.amount)] for r in qs]
    return render_list(request, title="Customer receipts", headers=["Receipt", "Customer", "Date", "Account", "Mode", "Amount"], rows=rows, add_url="/finance/receipts/new/", add_label="New receipt")


@role_required(*ROLES)
def receipt_new(request):
    b = get_current_business()
    form = ReceiptForm(request.POST or None, business=b)
    fs = InvAllocs(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        d = f.cleaned_data
        return services.create_receipt(business=b, user=request.user, customer_id=d["customer"].pk, account_id=d["account"].pk, amount=d["amount"],
                                       receipt_date=d["date"], mode=d["mode"], reference=d["reference"], allocations=_allocs(formsets["Allocate to invoices"]))
    return form_page(request, form, "New customer receipt", handler, "/finance/receipts/", formsets={"Allocate to invoices": fs}, back="/finance/receipts/",
                     note="Posts immediately and cannot be edited. Allocations cannot exceed an invoice's outstanding balance; unallocated money stays on account.")


@role_required(*ROLES)
def supplier_payments(request):
    b = get_current_business()
    qs = search_filter(SupplierPayment.objects.filter(business=b).select_related("supplier", "account"), request.GET.get("q", "").strip(), "number", "supplier__name")
    rows = [[p.number, p.supplier.name, p.payment_date, p.account.name, inr(p.amount)] for p in qs]
    return render_list(request, title="Supplier payments", headers=["Payment", "Supplier", "Date", "Account", "Amount"], rows=rows, add_url="/finance/supplier-payments/new/", add_label="New payment")


@role_required(*ROLES)
def supplier_payment_new(request):
    b = get_current_business()
    form = SupplierPaymentForm(request.POST or None, business=b)
    fs = BillAllocs(request.POST or None, form_kwargs={"business": b})

    def handler(f, formsets):
        d = f.cleaned_data
        return services.create_supplier_payment(business=b, user=request.user, supplier_id=d["supplier"].pk, account_id=d["account"].pk, amount=d["amount"],
                                                payment_date=d["date"], mode=d["mode"], reference=d["reference"], allocations=_allocs(formsets["Allocate to bills"]))
    return form_page(request, form, "New supplier payment", handler, "/finance/supplier-payments/", formsets={"Allocate to bills": fs}, back="/finance/supplier-payments/")


@role_required(*ROLES)
def expenses(request):
    b = get_current_business()
    qs = search_filter(Expense.objects.filter(business=b).select_related("account"), request.GET.get("q", "").strip(), "number", "description")
    rows = [[e.number, e.expense_date, e.get_category_display(), e.description, e.account.name, inr(e.amount)] for e in qs]
    return render_list(request, title="Expenses", headers=["No.", "Date", "Category", "Description", "Account", "Amount"], rows=rows, add_url="/finance/expenses/new/", add_label="New expense")


@role_required(*ROLES)
def expense_new(request):
    b = get_current_business()
    form = ExpenseForm(request.POST or None, business=b)

    def handler(f, fs):
        d = f.cleaned_data
        return services.create_expense(business=b, user=request.user, category=d["category"], amount=d["amount"], account_id=d["account"].pk,
                                       expense_date=d["date"], description=d["description"])
    return form_page(request, form, "New expense", handler, "/finance/expenses/", back="/finance/expenses/")


@role_required(*ROLES)
def cashbook(request):
    b = get_current_business()
    from finance.selectors import account_balances
    acc = request.GET.get("account")
    qs = CashBookEntry.objects.filter(business=b).select_related("account")
    if acc:
        qs = qs.filter(account_id=acc)
    rows = [[e.entry_date, e.account.name, e.narration, inr(e.amount) if e.direction == "in" else "", inr(e.amount) if e.direction == "out" else ""] for e in qs[:500]]
    bal = " · ".join(f"{r['account'].name}: {inr(r['balance'])}" for r in account_balances(b))
    return render_list(request, title="Cashbook", headers=["Date", "Account", "Narration", "Credit", "Debit"], rows=rows, search=False, subtitle="Balances - " + bal,
                       filters=[{"name": "account", "label": "Account", "current": acc or "", "options": [(a.pk, a.name) for a in FinancialAccount.objects.filter(business=b)]}])
