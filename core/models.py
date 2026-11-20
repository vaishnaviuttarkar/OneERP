from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from .exceptions import ImmutableRecordError
from .fields import money


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError("This record type is immutable.")

    def delete(self):
        raise ImmutableRecordError("This record type is immutable.")

    def bulk_update(self, *a, **k):
        raise ImmutableRecordError("This record type is immutable.")


class ImmutableModel(models.Model):
    """Rows can be inserted once and never changed or deleted through the ORM."""
    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(f"{self.__class__.__name__} records are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{self.__class__.__name__} records cannot be deleted.")


class PostedGuardModel(models.Model):
    """Documents with a `status` field: anything not draft cannot be saved again.
    Services change status with queryset.update() under a row lock."""
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding and getattr(self, "status", "draft") != "draft":
            raise ImmutableRecordError(f"Posted {self.__class__.__name__} is immutable.")
        super().save(*args, **kwargs)


class Business(models.Model):
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    slug = models.SlugField(unique=True)
    country = models.CharField(max_length=2, default="IN")
    currency = models.CharField(max_length=3, default="INR")
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    financial_year_start_month = models.PositiveSmallIntegerField(default=4)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=Q(financial_year_start_month__gte=1, financial_year_start_month__lte=12),
            name="business_fy_month_valid")]

    def __str__(self):
        return self.name


class BusinessSettings(models.Model):
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="settings")
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    tax_id = models.CharField(max_length=30, blank=True, help_text="Printed on documents only; no GST logic in MVP.")
    invoice_footer = models.TextField(blank=True)
    low_stock_threshold = models.DecimalField(max_digits=14, decimal_places=3, default=10)

    def __str__(self):
        return f"Settings for {self.business}"


class FinancialAccount(models.Model):
    CASH, BANK = "cash", "bank"
    KINDS = [(CASH, "Cash"), (BANK, "Bank")]
    business = models.ForeignKey(Business, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KINDS)
    opening_balance = money(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("name"), "business", name="uniq_finaccount_name_ci")]

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"


class AuditEvent(ImmutableModel):
    business = models.ForeignKey(Business, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=60)
    entity = models.CharField(max_length=60)
    entity_id = models.CharField(max_length=40)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    flagged = models.BooleanField(default=False, help_text="Flag for audit review (e.g. owner self-approval)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["business", "entity", "entity_id"])]


class DocumentSequence(models.Model):
    business = models.ForeignKey(Business, on_delete=models.PROTECT)
    doc_type = models.CharField(max_length=20)
    financial_year = models.CharField(max_length=7)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "doc_type", "financial_year"], name="uniq_docseq")]


class IdempotencyKey(models.Model):
    business = models.ForeignKey(Business, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    key = models.CharField(max_length=128)
    action = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "user", "key"], name="uniq_idempotency_key")]
