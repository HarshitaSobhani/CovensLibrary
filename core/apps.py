from django.apps import AppConfig
from django.db.models.signals import post_migrate


def apply_grants_after_migrate(sender, using="default", **kwargs):
    """Re-apply the runtime role's grants after every migrate (owner role only, idempotent)."""
    from django.conf import settings
    from django.db import connections

    from . import dbsecurity

    if settings.DB_ROLE != "owner" or not settings.DB_RUNTIME_ROLE:
        return
    connection = connections[using]
    with connection.cursor() as cur:
        cur.execute("SELECT current_user")
        if cur.fetchone()[0] == settings.DB_RUNTIME_ROLE:
            return  # explicit single-role development mode: nothing to grant to oneself
    dbsecurity.apply_grants(connection, settings.DB_RUNTIME_ROLE)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system check)

        post_migrate.connect(apply_grants_after_migrate, sender=self)
