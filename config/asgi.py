import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
application = get_asgi_application()

# Refuse to serve on an insecure database setup (strict two-role model; prints no secrets).
from core.dbsecurity import assert_runtime_security  # noqa: E402

assert_runtime_security()
