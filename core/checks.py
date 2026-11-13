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


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}


@checks.register()
def loopback_only(app_configs, **kwargs):
    """The application is reachable from this computer only (decision B37): refuse a configuration
    that accepts any other Host header."""
    bad = [h for h in settings.ALLOWED_HOSTS if h not in LOOPBACK_HOSTS]
    if not bad:
        return []
    return [
        checks.Error(
            f"ALLOWED_HOSTS may contain only loopback names (127.0.0.1, localhost); found {bad}.",
            hint="The application must never be reachable from the network (B37).",
            id="coven.E010",
        )
    ]
