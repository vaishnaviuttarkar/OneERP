import hashlib
import json

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .exceptions import DomainError, IdempotencyConflict
from .models import AuditEvent, Business, DocumentSequence, IdempotencyKey
from .utils import financial_year

DOC_PREFIX = {
    "purchase_bill": "PB", "delivery_challan": "DC", "sales_invoice": "INV", "receipt": "RCT",
    "supplier_payment": "SPAY", "expense": "EXP", "batch": "BATCH", "wage_statement": "WS", "enquiry": "ENQ",
}


def get_current_business():
    """Single place that resolves the active business (multi-business ready)."""
    qs = Business.objects.filter(is_active=True)
    slug = getattr(settings, "DEFAULT_BUSINESS_SLUG", "")
    if slug:
        qs = qs.filter(slug=slug)
    business = qs.order_by("id").first()
    if business is None:
        raise DomainError("No business configured. Run: python manage.py create_owner")
    return business


def next_number(business, doc_type, on_date=None):
    """Issue the next document number. Locks the sequence row; numbers are never reused."""
    on_date = on_date or timezone.localdate()
    fy = financial_year(on_date, business.financial_year_start_month)
    with transaction.atomic():
        seq = DocumentSequence.objects.select_for_update().filter(
            business=business, doc_type=doc_type, financial_year=fy).first()
        if seq is None:
            try:
                with transaction.atomic():
                    DocumentSequence.objects.create(business=business, doc_type=doc_type, financial_year=fy)
            except IntegrityError:
                pass
            seq = DocumentSequence.objects.select_for_update().get(
                business=business, doc_type=doc_type, financial_year=fy)
        seq.last_number += 1
        seq.save(update_fields=["last_number"])
    return f"{DOC_PREFIX[doc_type]}/{fy}/{seq.last_number:05d}"


def record_audit(*, business, user, action, entity, entity_id, before=None, after=None, flagged=False):
    def clean(v):
        return None if v is None else json.loads(json.dumps(v, default=str))
    return AuditEvent.objects.create(
        business=business, user=user, action=action, entity=entity, entity_id=str(entity_id),
        before=clean(before), after=clean(after), flagged=flagged)


def _lock_id(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big", signed=True)


def run_idempotent(*, business, user, key, action, payload, func):
    """Returns (result_dict, replayed_bool). Only successful operations store the key:
    func runs inside the same transaction, so a failure rolls back the key too."""
    if not key:
        return func(), False
    request_hash = hashlib.sha256(json.dumps(
        {"action": action, "payload": payload}, sort_keys=True, default=str).encode()).hexdigest()
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [_lock_id(business.id, user.id, key)])
        existing = IdempotencyKey.objects.filter(business=business, user=user, key=key).first()
        if existing:
            if existing.request_hash != request_hash:
                raise IdempotencyConflict("Idempotency key was already used with a different request.")
            return existing.response, True
        result = func()
        IdempotencyKey.objects.create(business=business, user=user, key=key, action=action,
                                      request_hash=request_hash, response=result)
        return result, False
