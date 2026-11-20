from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User
from core.models import Business, BusinessSettings, FinancialAccount


class Command(BaseCommand):
    help = "Create the business (if missing) and an Owner user. Prompts for a password."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="owner")
        parser.add_argument("--password", default=None, help="If omitted you are prompted.")
        parser.add_argument("--business", default="My Uniform Works")

    @transaction.atomic
    def handle(self, *args, **o):
        import getpass
        from django.utils.text import slugify
        b = Business.objects.first()
        if not b:
            b = Business.objects.create(name=o["business"], slug=slugify(o["business"]) or "business")
            BusinessSettings.objects.create(business=b)
            FinancialAccount.objects.create(business=b, name="Cash in Hand", kind="cash")
            FinancialAccount.objects.create(business=b, name="Bank Account", kind="bank")
            self.stdout.write(f"Created business {b.name}")
        pw = o["password"] or getpass.getpass("Owner password: ")
        u, created = User.objects.get_or_create(username=o["username"], defaults={"role": User.OWNER})
        u.role = User.OWNER
        u.set_password(pw)
        u.save()
        self.stdout.write(self.style.SUCCESS(f"Owner '{u.username}' {'created' if created else 'updated'}."))
