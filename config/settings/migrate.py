"""Settings for schema work: migrations, grants and maintenance run as the OWNER role.

Usage:  python manage.py migrate --settings=config.settings.migrate
Reads DB_OWNER_USER / DB_OWNER_PASSWORD (owner) and DB_USER (only the runtime role's NAME, so the
grants can be applied to it). The runtime password is not needed here.
"""

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

try:  # development convenience; absent (and unused) on the Windows runtime machine
    from dotenv import load_dotenv

    _root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_root / ".env")
    load_dotenv(_root / ".env.owner")  # owner credentials live in their own file (development)
except ImportError:  # pragma: no cover
    pass

from .base import *  # noqa: E402,F401,F403
from .base import DB_SECURITY_MODE, build_database, env  # noqa: E402

DB_ROLE = "owner"
_owner = env("DB_OWNER_USER", required=True)
DB_RUNTIME_ROLE = env("DB_USER", required=True)
if _owner == DB_RUNTIME_ROLE and DB_SECURITY_MODE == "strict":
    raise ImproperlyConfigured(
        "DB_OWNER_USER and DB_USER must be different roles (set DB_SECURITY_MODE=single_role_dev "
        "only on a development machine)."
    )
DATABASES = build_database(_owner, env("DB_OWNER_PASSWORD", required=True))
