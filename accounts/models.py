from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    OWNER, ACCOUNTS, PRODUCTION = "owner", "accounts", "production"
    ROLES = [(OWNER, "Owner"), (ACCOUNTS, "Accounts/Billing"), (PRODUCTION, "Production")]
    role = models.CharField(max_length=20, choices=ROLES, default=PRODUCTION)

    @property
    def effective_role(self):
        return self.OWNER if self.is_superuser else self.role

    @property
    def is_owner(self):
        return self.effective_role == self.OWNER
