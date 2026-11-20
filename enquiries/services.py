from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.exceptions import DomainError
from core.services import next_number, record_audit
from . import mailer
from .models import Enquiry, EnquiryNote, EnquiryReply


class RateLimited(DomainError):
    pass


def _notification_text(e):
    return (f"New business enquiry {e.reference}\n"
            f"{'-' * 44}\n"
            f"Type:     {e.get_enquiry_type_display()}\n"
            f"Name:     {e.name}\n"
            f"Company:  {e.company or '-'}\n"
            f"Email:    {e.email}\n"
            f"Phone:    {e.phone or '-'}\n"
            f"Subject:  {e.subject}\n\n"
            f"{e.message}\n\n"
            f"{'-' * 44}\n"
            f"Reply to this e-mail to answer {e.name} directly, or manage the enquiry in the ERP.\n")


def notify_company(enquiry):
    """E-mail the enquiry to the business mailbox and record the outcome on the enquiry."""
    result = mailer.send(subject=f"[{enquiry.reference}] {enquiry.get_enquiry_type_display()} enquiry: {enquiry.subject}",
                         body=_notification_text(enquiry), to=[mailer.business_mailbox()] if mailer.business_mailbox() else [],
                         reply_to=[enquiry.email], headers={"X-Enquiry-Reference": enquiry.reference})
    Enquiry.objects.filter(pk=enquiry.pk).update(notified_at=timezone.now() if result.ok else None,
                                                  notify_error="" if result.ok else result.error)
    enquiry.refresh_from_db()
    return result


def submit_enquiry(*, business, data, ip=None):
    """Public submission. Returns (enquiry, created). The enquiry is stored BEFORE any e-mail is attempted,
    so an SMTP failure can never lose a customer's message."""
    now = timezone.now()
    dup = Enquiry.objects.filter(business=business, email__iexact=data["email"], subject=data["subject"], message=data["message"],
                                 submitted_at__gte=now - timedelta(minutes=10)).first()
    if dup:                                    # double click / refresh: return the original reference
        return dup, False
    limit = settings.ENQUIRY_RATE_LIMIT_PER_HOUR
    if ip and limit and Enquiry.objects.filter(business=business, source_ip=ip, submitted_at__gte=now - timedelta(hours=1)).count() >= limit:
        raise RateLimited("Too many enquiries have been sent from your network recently. Please try again later.")
    with transaction.atomic():
        reference = next_number(business, "enquiry")
        enquiry = Enquiry.objects.create(
            business=business, reference=reference, name=data["name"], company=data.get("company", ""), email=data["email"],
            phone=data.get("phone", ""), enquiry_type=data["enquiry_type"], subject=data["subject"], message=data["message"],
            source_ip=ip or None)
        record_audit(business=business, user=None, action="enquiry.received", entity="Enquiry", entity_id=enquiry.pk,
                     after={"reference": reference, "type": enquiry.enquiry_type})
    notify_company(enquiry)                    # outside the transaction: SMTP latency must not hold DB locks
    return enquiry, True


def resend_notification(*, business, user, enquiry_id):
    enquiry = Enquiry.objects.get(pk=enquiry_id, business=business)
    result = notify_company(enquiry)
    record_audit(business=business, user=user, action="enquiry.resend_notification", entity="Enquiry",
                 entity_id=enquiry.pk, after={"ok": result.ok})
    return result


@transaction.atomic
def change_status(*, business, user, enquiry_id, status):
    if status not in dict(Enquiry.STATUSES):
        raise DomainError("Invalid status.")
    enquiry = Enquiry.objects.select_for_update().get(pk=enquiry_id, business=business)
    if enquiry.status == status:
        raise DomainError(f"The enquiry is already {enquiry.get_status_display()}.")
    before = enquiry.status
    Enquiry.objects.filter(pk=enquiry.pk).update(status=status, updated_at=timezone.now(),
                                                 closed_at=timezone.now() if status == Enquiry.CLOSED else None)
    record_audit(business=business, user=user, action="enquiry.status", entity="Enquiry", entity_id=enquiry.pk,
                 before={"status": before}, after={"status": status})
    enquiry.refresh_from_db()
    return enquiry


@transaction.atomic
def assign(*, business, user, enquiry_id, assignee):
    """assignee: a User (Owner/Accounts) or None to unassign."""
    enquiry = Enquiry.objects.select_for_update().get(pk=enquiry_id, business=business)
    if assignee is not None and (not assignee.is_active or assignee.effective_role not in ("owner", "accounts")):
        raise DomainError("Enquiries can only be assigned to active Owner or Accounts users.")
    before = enquiry.assigned_to.username if enquiry.assigned_to else None
    update = {"assigned_to": assignee, "updated_at": timezone.now()}
    if assignee is not None and enquiry.status == Enquiry.NEW:
        update["status"] = Enquiry.IN_PROGRESS
    Enquiry.objects.filter(pk=enquiry.pk).update(**update)
    record_audit(business=business, user=user, action="enquiry.assign", entity="Enquiry", entity_id=enquiry.pk,
                 before={"assigned_to": before}, after={"assigned_to": assignee.username if assignee else None})
    enquiry.refresh_from_db()
    return enquiry


@transaction.atomic
def add_note(*, business, user, enquiry_id, body):
    body = (body or "").strip()
    if not body:
        raise DomainError("Write something in the note.")
    enquiry = Enquiry.objects.select_for_update().get(pk=enquiry_id, business=business)
    note = EnquiryNote.objects.create(enquiry=enquiry, author=user, body=body)
    record_audit(business=business, user=user, action="enquiry.note", entity="Enquiry", entity_id=enquiry.pk)
    return note


def default_reply_subject(enquiry):
    return f"Re: [{enquiry.reference}] {enquiry.subject}"[:200]


def send_reply(*, business, user, enquiry_id, subject, body):
    """Send a reply from the business mailbox and keep it in the history. Returns the EnquiryReply
    (check .delivered). A failed send is recorded too, and the status is NOT advanced."""
    subject = " ".join((subject or "").split())
    body = (body or "").strip()
    if not subject or len(body) < 2:
        raise DomainError("A subject and a message are required.")
    enquiry = Enquiry.objects.get(pk=enquiry_id, business=business)
    signature = f"\n\n--\n{business.name}\n"
    result = mailer.send(subject=subject, body=body + signature, to=[enquiry.email], reply_to=[mailer.business_mailbox()],
                         headers={"X-Enquiry-Reference": enquiry.reference})
    with transaction.atomic():
        reply = EnquiryReply.objects.create(enquiry=enquiry, sent_by=user, to_email=enquiry.email, subject=subject, body=body,
                                            delivered=result.ok, error="" if result.ok else result.error)
        locked = Enquiry.objects.select_for_update().get(pk=enquiry.pk)
        if result.ok and locked.status in (Enquiry.NEW, Enquiry.IN_PROGRESS):
            Enquiry.objects.filter(pk=enquiry.pk).update(status=Enquiry.RESPONDED, updated_at=timezone.now())
        record_audit(business=business, user=user, action="enquiry.reply", entity="Enquiry", entity_id=enquiry.pk,
                     after={"delivered": result.ok, "to": enquiry.email})
    return reply
