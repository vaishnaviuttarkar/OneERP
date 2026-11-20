from django.db import models
from django.db.models import Q

from core.models import ImmutableModel


class Enquiry(models.Model):
    SALES, PRODUCT, PRICING, PARTNERSHIP, SUPPORT, OTHER = "sales", "product", "pricing", "partnership", "support", "other"
    TYPES = [(SALES, "Sales"), (PRODUCT, "Product"), (PRICING, "Pricing"), (PARTNERSHIP, "Partnership"),
             (SUPPORT, "Support"), (OTHER, "Other")]
    NEW, IN_PROGRESS, RESPONDED, CLOSED = "new", "in_progress", "responded", "closed"
    STATUSES = [(NEW, "New"), (IN_PROGRESS, "In Progress"), (RESPONDED, "Responded"), (CLOSED, "Closed")]

    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    reference = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    company = models.CharField(max_length=150, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    enquiry_type = models.CharField(max_length=15, choices=TYPES)
    subject = models.CharField(max_length=150)
    message = models.TextField()
    status = models.CharField(max_length=12, choices=STATUSES, default=NEW)
    assigned_to = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="assigned_enquiries")
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    # delivery of the notification e-mail to the business mailbox (generic text only - never credentials)
    notified_at = models.DateTimeField(null=True, blank=True)
    notify_error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        verbose_name_plural = "enquiries"
        constraints = [
            models.UniqueConstraint(fields=["business", "reference"], name="uniq_enquiry_reference"),
            models.CheckConstraint(condition=Q(status__in=["new", "in_progress", "responded", "closed"]), name="enquiry_status_valid"),
            models.CheckConstraint(condition=Q(enquiry_type__in=["sales", "product", "pricing", "partnership", "support", "other"]),
                                   name="enquiry_type_valid"),
        ]
        indexes = [models.Index(fields=["business", "status", "-submitted_at"]), models.Index(fields=["source_ip", "submitted_at"])]

    def __str__(self):
        return f"{self.reference} - {self.subject}"


class EnquiryNote(ImmutableModel):
    """Internal note (never e-mailed). Insert-only so the conversation history cannot be rewritten."""
    enquiry = models.ForeignKey(Enquiry, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class EnquiryReply(ImmutableModel):
    """A reply composed in the ERP and sent from the business mailbox (delivered=False if SMTP failed)."""
    enquiry = models.ForeignKey(Enquiry, on_delete=models.PROTECT, related_name="replies")
    sent_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    to_email = models.EmailField()
    subject = models.CharField(max_length=200)
    body = models.TextField()
    delivered = models.BooleanField(default=False)
    error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
