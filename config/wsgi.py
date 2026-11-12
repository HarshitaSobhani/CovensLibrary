import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
application = get_wsgi_application()

# Refuse to serve on an insecure database setup (strict two-role model; prints no secrets).
from core.dbsecurity import assert_runtime_security  # noqa: E402

assert_runtime_security()
