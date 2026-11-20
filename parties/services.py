import re

from django.db import IntegrityError, transaction

from core.exceptions import DomainError
from core.services import record_audit
from .models import Party


def _name(n):
    n = re.sub(r"\s+", " ", (n or "").strip())
    if not n:
        raise DomainError("Party name is required.")
    return n


@transaction.atomic
def create_party(*, business, user, name, is_customer=False, is_supplier=False, **extra):
    name = _name(name)
    if Party.objects.filter(business=business, name__iexact=name).exists():
        raise DomainError(f"A party named '{name}' already exists.")
    try:
        with transaction.atomic():
            p = Party.objects.create(business=business, name=name, is_customer=is_customer,
                                     is_supplier=is_supplier, **extra)
    except IntegrityError:
        raise DomainError(f"A party named '{name}' already exists.")
    record_audit(business=business, user=user, action="party.create", entity="Party", entity_id=p.pk,
                 after={"name": p.name, "roles": p.roles_label})
    return p


@transaction.atomic
def update_party(*, business, user, party_id, name, is_customer=False, is_supplier=False, **extra):
    p = Party.objects.select_for_update().get(pk=party_id, business=business)
    name = _name(name)
    if Party.objects.filter(business=business, name__iexact=name).exclude(pk=p.pk).exists():
        raise DomainError(f"A party named '{name}' already exists.")
    # keep history valid: a party with documents cannot lose the role those documents depend on
    if p.is_customer and not is_customer and p.invoices.exists():
        raise DomainError("This party has invoices and must stay a customer.")
    if p.is_supplier and not is_supplier and p.purchase_bills.exists():
        raise DomainError("This party has purchase bills and must stay a supplier.")
    before = {"name": p.name, "roles": p.roles_label}
    p.name, p.is_customer, p.is_supplier = name, is_customer, is_supplier
    for k in ("contact_person", "phone", "email", "address", "tax_id"):
        setattr(p, k, extra.get(k) or "")
    p.save()
    record_audit(business=business, user=user, action="party.update", entity="Party", entity_id=p.pk, before=before,
                 after={"name": p.name, "roles": p.roles_label})
    return p


@transaction.atomic
def delete_party(*, business, user, party_id):
    p = Party.objects.select_for_update().get(pk=party_id, business=business)
    if p.invoices.exists() or p.challans.exists() or p.receipts.exists() or p.purchase_bills.exists() or p.payments.exists() or p.batches.exists():
        raise DomainError(f"{p.name} has documents or batches and cannot be deleted. Deactivate it instead.")
    record_audit(business=business, user=user, action="party.delete", entity="Party", entity_id=p.pk, before={"name": p.name})
    p.delete()


@transaction.atomic
def set_party_active(*, business, user, party_id, active):
    p = Party.objects.select_for_update().get(pk=party_id, business=business)
    p.is_active = active
    p.save(update_fields=["is_active"])
    record_audit(business=business, user=user, action="party.active", entity="Party", entity_id=p.pk,
                 after={"is_active": active})
    return p


def require_party(business, party_id, *, customer=False, supplier=False, lock=False):
    """Validate a party can be used in a financial/stock document."""
    qs = Party.objects.filter(business=business, pk=party_id)
    if lock:
        qs = qs.select_for_update()
    p = qs.first()
    if p is None:
        raise DomainError("Party not found.")
    if not p.is_active:
        raise DomainError(f"{p.name} is deactivated and cannot be used in new documents.")
    if customer and not p.is_customer:
        raise DomainError(f"{p.name} is not a customer.")
    if supplier and not p.is_supplier:
        raise DomainError(f"{p.name} is not a supplier.")
    return p
