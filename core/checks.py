from django.conf import settings
from django.core import checks
from django.db import connection
from django.db.utils import OperationalError

from . import dbsecurity


@checks.register()
def database_security(app_configs, **kwargs):
    """System check: the database must match the two-role security model (see dbsecurity)."""
    if not settings.DB_RUNTIME_ROLE:
        return []
    kind = "owner" if settings.DB_ROLE == "owner" else "runtime"
    try:
        findings = dbsecurity.inspect(connection, settings.DB_RUNTIME_ROLE, kind)
    except OperationalError:
        return [
            checks.Warning("Could not inspect database security (unreachable).", id="coven.W001")
        ]
    out = []
    for f in findings:
        level = checks.Error if f.level == "error" else checks.Warning
        out.append(level(f.message, id=f.code))
    return out
