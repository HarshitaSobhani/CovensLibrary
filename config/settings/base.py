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


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", False)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# Email (SMTP). Credentials come only from the environment. Local development prints to the console.
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_TIMEOUT = int(env("EMAIL_TIMEOUT", "15"))
NOTIFY_AFTER_WRITE = env_bool("NOTIFY_AFTER_WRITE", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "noreply@localhost")
