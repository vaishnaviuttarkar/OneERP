import sys

from django.core.management.base import BaseCommand

from core.services import get_current_business
from reports.reconciliation import run_all


class Command(BaseCommand):
    help = "Run read-only reconciliation checks. Exit code 1 if any discrepancy. Never repairs data."

    def handle(self, *args, **opts):
        failed = 0
        for c in run_all(get_current_business()):
            self.stdout.write(f"[{'OK  ' if c.ok else 'FAIL'}] {c.group}: {c.name}")
            for i in c.issues:
                self.stdout.write(f"       - {i}")
            failed += 0 if c.ok else 1
        if failed:
            self.stdout.write(self.style.ERROR(f"{failed} check(s) failed."))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("All checks passed."))
