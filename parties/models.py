import re

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower, Trim


class Party(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    is_customer = models.BooleanField(default=False)
    is_supplier = models.BooleanField(default=False)
    contact_person = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    tax_id = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "parties"
        constraints = [
            models.UniqueConstraint(Lower("name"), "business", name="uniq_party_name_ci"),
            models.CheckConstraint(condition=Q(name=Trim("name")) & ~Q(name=""), name="party_name_trimmed"),
        ]

    def save(self, *args, **kwargs):
        self.name = re.sub(r"\s+", " ", (self.name or "").strip())
        super().save(*args, **kwargs)

    @property
    def roles_label(self):
        r = []
        if self.is_customer:
            r.append("Customer")
        if self.is_supplier:
            r.append("Supplier")
        return " & ".join(r) or "Other"

    def __str__(self):
        return self.name
