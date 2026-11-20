from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.exceptions import DomainError
from core.models import FinancialAccount
from core.services import next_number, record_audit
from core.utils import D, ZERO
from parties.services import require_party
from purchasing.models import PurchaseBill
from sales.models import SalesInvoice
from .models import (CashBookEntry, Expense, Receipt, ReceiptAllocation, SupplierPayment,
                     SupplierPaymentAllocation)


def get_account(business, account_id):
    acc = FinancialAccount.objects.filter(pk=account_id, business=business).first()
    if acc is None or not acc.is_active:
        raise DomainError("Select an active cash/bank account.")
    return acc


def add_cashbook(*, business, account, entry_date, direction, amount, narration="", **source):
    return CashBookEntry.objects.create(business=business, account=account, entry_date=entry_date,
                                        direction=direction, amount=amount, narration=narration, **source)


def _check_date(d, label="Date"):
    if d > timezone.localdate():
        raise DomainError(f"{label} cannot be in the future.")


def _allocate(business, allocations, total_amount, *, model, party_field, party, doc_status, paid_related, label):
    """Validate allocations under row locks; returns [(locked_doc, amount)]."""
    allocations = [(a["doc_id"], D(a["amount"])) for a in (allocations or []) if D(a.get("amount")) > 0]
    if sum((a for _, a in allocations), ZERO) > total_amount:
        raise DomainError("Allocated amount exceeds the amount paid/received.")
    ids = [i for i, _ in allocations]
    if len(ids) != len(set(ids)):
        raise DomainError("Duplicate document in allocations.")
    docs = {d.pk: d for d in model.objects.select_for_update().filter(pk__in=ids, business=business).order_by("id")}
    out = []
    for doc_id, amt in allocations:
        doc = docs.get(doc_id)
        if doc is None or doc.status != doc_status:
            raise DomainError(f"{label} not found or not posted.")
        if getattr(doc, party_field + "_id") != party.pk:
            raise DomainError(f"{label} {doc.number} belongs to a different party.")
        done = getattr(doc, paid_related).aggregate(t=Sum("amount"))["t"] or ZERO
        outstanding = doc.total - done
        if amt > outstanding:
            raise DomainError(f"{label} {doc.number}: allocation {amt} exceeds outstanding {outstanding}.")
        out.append((doc, amt))
    return out


@transaction.atomic
def create_receipt(*, business, user, customer_id, account_id, amount, receipt_date, mode="cash",
                   reference="", allocations=None):
    cust = require_party(business, customer_id, customer=True)
    acc = get_account(business, account_id)
    amount = D(amount)
    if amount <= 0:
        raise DomainError("Receipt amount must be positive.")
    _check_date(receipt_date, "Receipt date")
    checked = _allocate(business, allocations, amount, model=SalesInvoice, party_field="customer", party=cust,
                        doc_status="posted", paid_related="receipt_allocations", label="Invoice")
    number = next_number(business, "receipt", receipt_date)
    r = Receipt.objects.create(business=business, number=number, customer=cust, account=acc, amount=amount,
                               receipt_date=receipt_date, mode=mode, reference=reference, created_by=user)
    for inv, amt in checked:
        ReceiptAllocation.objects.create(business=business, receipt=r, invoice=inv, amount=amt)
    add_cashbook(business=business, account=acc, entry_date=receipt_date, direction="in", amount=amount,
                 narration=f"Receipt {number} - {cust.name}", receipt=r)
    record_audit(business=business, user=user, action="receipt.create", entity="Receipt", entity_id=r.pk,
                 after={"number": number, "amount": amount, "allocated": sum((a for _, a in checked), ZERO)})
    return r


@transaction.atomic
def allocate_receipt(*, business, user, receipt_id, allocations):
    """Allocate the unallocated remainder of an existing receipt to invoices."""
    r = Receipt.objects.select_for_update().get(pk=receipt_id, business=business)
    used = r.allocations.aggregate(t=Sum("amount"))["t"] or ZERO
    checked = _allocate(business, allocations, r.amount - used, model=SalesInvoice, party_field="customer",
                        party=r.customer, doc_status="posted", paid_related="receipt_allocations", label="Invoice")
    for inv, amt in checked:
        ReceiptAllocation.objects.create(business=business, receipt=r, invoice=inv, amount=amt)
    record_audit(business=business, user=user, action="receipt.allocate", entity="Receipt", entity_id=r.pk)
    return r


@transaction.atomic
def create_supplier_payment(*, business, user, supplier_id, account_id, amount, payment_date, mode="bank",
                            reference="", allocations=None):
    sup = require_party(business, supplier_id, supplier=True)
    acc = get_account(business, account_id)
    amount = D(amount)
    if amount <= 0:
        raise DomainError("Payment amount must be positive.")
    _check_date(payment_date, "Payment date")
    checked = _allocate(business, allocations, amount, model=PurchaseBill, party_field="supplier", party=sup,
                        doc_status="posted", paid_related="payment_allocations", label="Purchase bill")
    number = next_number(business, "supplier_payment", payment_date)
    p = SupplierPayment.objects.create(business=business, number=number, supplier=sup, account=acc, amount=amount,
                                       payment_date=payment_date, mode=mode, reference=reference, created_by=user)
    for bill, amt in checked:
        SupplierPaymentAllocation.objects.create(business=business, payment=p, bill=bill, amount=amt)
    add_cashbook(business=business, account=acc, entry_date=payment_date, direction="out", amount=amount,
                 narration=f"Payment {number} - {sup.name}", supplier_payment=p)
    record_audit(business=business, user=user, action="supplier_payment.create", entity="SupplierPayment",
                 entity_id=p.pk, after={"number": number, "amount": amount})
    return p


@transaction.atomic
def allocate_supplier_payment(*, business, user, payment_id, allocations):
    p = SupplierPayment.objects.select_for_update().get(pk=payment_id, business=business)
    used = p.allocations.aggregate(t=Sum("amount"))["t"] or ZERO
    checked = _allocate(business, allocations, p.amount - used, model=PurchaseBill, party_field="supplier",
                        party=p.supplier, doc_status="posted", paid_related="payment_allocations", label="Purchase bill")
    for bill, amt in checked:
        SupplierPaymentAllocation.objects.create(business=business, payment=p, bill=bill, amount=amt)
    record_audit(business=business, user=user, action="supplier_payment.allocate", entity="SupplierPayment", entity_id=p.pk)
    return p


@transaction.atomic
def create_expense(*, business, user, category, amount, account_id, expense_date, description=""):
    acc = get_account(business, account_id)
    amount = D(amount)
    if amount <= 0:
        raise DomainError("Expense amount must be positive.")
    if category not in dict(Expense.CATEGORIES):
        raise DomainError("Invalid expense category.")
    _check_date(expense_date, "Expense date")
    number = next_number(business, "expense", expense_date)
    e = Expense.objects.create(business=business, number=number, category=category, description=description,
                               account=acc, amount=amount, expense_date=expense_date, created_by=user)
    add_cashbook(business=business, account=acc, entry_date=expense_date, direction="out", amount=amount,
                 narration=f"Expense {number} - {e.get_category_display()}", expense=e)
    record_audit(business=business, user=user, action="expense.create", entity="Expense", entity_id=e.pk,
                 after={"number": number, "amount": amount})
    return e
