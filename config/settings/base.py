"""Shared settings. All configuration comes from environment variables (no insecure defaults)."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise ImproperlyConfigured(f"Required environment variable {name} is not set.")
    return value


STORE_MODE = os.environ.get("COVEN_SECRET_STORE") == "credential-manager"


def secret(name, default=None, required=False):
    """A secret. Production (``COVEN_SECRET_STORE=credential-manager``): ONLY the Windows Credential
    Manager. A plaintext environment variable of the same name is REFUSED (not used as a fallback),
    and a store that cannot be read stops the application. Development and tests: the environment
    (``.env`` on the developer's machine)."""
    if STORE_MODE:
        from config import secretstore

        if os.environ.get(name):
            raise ImproperlyConfigured(
                f"{name} is set as an environment variable. In production secrets are read only "
                "from the Windows Credential Manager: remove the variable."
            )
        try:
            value = secretstore.get_checked(name)
        except secretstore.SecretStoreError as exc:
            raise ImproperlyConfigured(f"{exc}. The application will not start.") from exc
        if not value and required:
            raise ImproperlyConfigured(
                f"Required secret {name} is not saved in the Windows Credential Manager "
                "(run deploy\\windows\\store_secrets.py)."
            )
        return value or default
    value = os.environ.get(name) or default
    if required and not value:
        raise ImproperlyConfigured(f"Required secret {name} is not set.")
    return value


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = secret("DJANGO_SECRET_KEY", required=True)
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "accounts",
    "core",
    "audit",
    "members",
    "catalogue",
    "finance",
    "memberships",
    "circulation",
    "communications",
    "reports",
    "backups",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "communications.middleware.NotifyAfterWriteMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.library",
            ],
        },
    },
]

# --- Database: two roles (see docs/DATABASE_SECURITY.md) -------------------------------------
# coven_app   = RUNTIME role: data access only (DB_USER / DB_PASSWORD).
# coven_owner = OWNER role: owns the objects, runs migrations (DB_OWNER_USER / DB_OWNER_PASSWORD).
# Owner credentials are read ONLY by config.settings.migrate and config.settings.test, never by
# the runtime settings, so they never need to exist in the runtime environment.
DB_SECURITY_MODE = env("DB_SECURITY_MODE", "strict")
if DB_SECURITY_MODE not in {"strict", "single_role_dev"}:
    raise ImproperlyConfigured("DB_SECURITY_MODE must be 'strict' or 'single_role_dev'.")
DB_RUNTIME_ROLE = env("DB_USER", "")  # the runtime role NAME (not a secret)
DB_ROLE = "runtime"  # overridden to "owner" by the migrate and test settings


def build_database(user, password):
    return {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", required=True),
            "USER": user,
            "PASSWORD": password,
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
        }
    }


AUTH_USER_MODEL = "accounts.User"
# Password-reset link lifetime: 1 hour (3600 s), as decided by the owner (Django's default would be
# 3 days). Set explicitly so it is visible, tested and cannot change silently with an upgrade.
PASSWORD_RESET_TIMEOUT = 3600
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-in"
APP_VERSION = "0.7.0"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

MEDIA_ROOT = BASE_DIR / "media"
STATIC_URL = "static/"
STATIC_ROOT = Path(env("STATIC_ROOT", str(BASE_DIR / "staticfiles")))
STATICFILES_DIRS = [BASE_DIR / "static"]
WHITENOISE_USE_FINDERS = (
    DEBUG  # development serves straight from static/; production uses collectstatic
)
WHITENOISE_AUTOREFRESH = DEBUG
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", False)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_AGE = None  # a session cookie: not kept for the Django default of one year
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# Email (SMTP). Credentials come only from the environment. Local development prints to the console.
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = secret("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = int(env("EMAIL_TIMEOUT", "15"))
NOTIFY_AFTER_WRITE = env_bool("NOTIFY_AFTER_WRITE", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "noreply@localhost")

# Backups (Phase 7a). Secrets are never configured here: the working key and Google tokens live in
# the Windows Credential Manager (backups/keystore.py).
BACKUP_KEYSTORE = env("BACKUP_KEYSTORE", "keyring")  # "memory" only in tests
BACKUP_DESTINATION = env("BACKUP_DESTINATION", "drive")  # "drive" or "local:<folder>"
PG_DUMP_PATH = env("PG_DUMP_PATH", "pg_dump")
BACKUP_TMP_DIR = env("BACKUP_TMP_DIR", "")  # empty = the system temporary folder

# Where the launcher keeps its process-state files and where the protected application log lives
# (both set by the Windows launcher; the defaults suit development and tests).
COVEN_RUN_DIR = env("COVEN_RUN_DIR", str(BASE_DIR / "run"))
COVEN_LOG_DIR = env("COVEN_LOG_DIR", "")  # empty = log to the console only
