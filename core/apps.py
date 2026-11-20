from django.apps import AppConfig


def ensure_btree_gist(sender, using="default", **kwargs):
    """Infrastructure only: exclusion constraints on payroll need btree_gist.
    Runs before migrations on every database (including the test database)."""
    from django.db import connections
    conn = connections[using]
    if conn.vendor == "postgresql":
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from django.db.models.signals import pre_migrate
        pre_migrate.connect(ensure_btree_gist, sender=self)
