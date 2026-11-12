import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from dotenv import load_dotenv  # isort: skip

_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_root / ".env")
load_dotenv(_root / ".env.owner")  # development only: owner credentials for the test suite
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-0123456789abcdef0123456789abcd")

from .base import *  # noqa: E402,F401,F403
from .base import DB_SECURITY_MODE, build_database, env  # noqa: E402

# Tests create and drop a test database and flush tables (TRUNCATE), so they run as the OWNER role.
DB_ROLE = "owner"
if os.environ.get("DB_OWNER_USER"):
    DATABASES = build_database(env("DB_OWNER_USER"), env("DB_OWNER_PASSWORD", required=True))
elif DB_SECURITY_MODE == "single_role_dev":
    DATABASES = build_database(env("DB_USER", required=True), env("DB_PASSWORD", required=True))
else:
    raise ImproperlyConfigured(
        "Tests need DB_OWNER_USER/DB_OWNER_PASSWORD "
        "(or an explicit DB_SECURITY_MODE=single_role_dev)."
    )

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
