from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import DB_SECURITY_MODE, build_database, env

# Production-like configuration: the strict two-role model is mandatory. There is no silent
# fallback to a single database role, and no owner credentials are read here.
if DB_SECURITY_MODE != "strict":
    raise ImproperlyConfigured("The production settings require DB_SECURITY_MODE=strict.")
DB_ROLE = "runtime"
DATABASES = build_database(env("DB_USER", required=True), env("DB_PASSWORD", required=True))
