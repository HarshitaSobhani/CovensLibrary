import os

from django.core.exceptions import ImproperlyConfigured

# Production reads its secrets ONLY from the Windows Credential Manager. This check runs before the
# shared settings are imported, so a missing declaration can never fall back to the environment.
if os.environ.get("COVEN_SECRET_STORE") != "credential-manager":
    raise ImproperlyConfigured(
        "The production settings require COVEN_SECRET_STORE=credential-manager (set by the "
        "launcher); secrets are never read from environment variables in production."
    )

from core.safelog import build_logging  # noqa: E402

from .base import *  # noqa: E402,F401,F403
from .base import DB_SECURITY_MODE, DEBUG, build_database, env, secret  # noqa: E402

# Production-like configuration: the strict two-role model is mandatory. There is no silent
# fallback to a single database role, and no owner credentials are read here.
if DB_SECURITY_MODE != "strict":
    raise ImproperlyConfigured("The production settings require DB_SECURITY_MODE=strict.")
if DEBUG:
    # DEBUG shows tracebacks, settings and SQL in the browser; it must never run on the Dell.
    raise ImproperlyConfigured("DJANGO_DEBUG must not be enabled with the production settings.")
DB_ROLE = "runtime"
DATABASES = build_database(env("DB_USER", required=True), secret("DB_PASSWORD", required=True))
LOGGING = build_logging(COVEN_LOG_DIR)  # noqa: F405
