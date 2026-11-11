import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

BASE = Path(__file__).resolve().parent.parent


def run_settings_probe(env_extra, code):
    """Import production settings in a clean subprocess with a controlled environment."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
    }
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BASE, env=env, capture_output=True, text=True
    )


FULL_ENV = {
    "DJANGO_SECRET_KEY": "x" * 60,
    "DB_NAME": "n",
    "DB_USER": "u",
    "DB_PASSWORD": "p",
}


class SettingsTests(SimpleTestCase):
    def test_timezone_and_tz_awareness(self):
        self.assertEqual(settings.TIME_ZONE, "Asia/Kolkata")
        self.assertTrue(settings.USE_TZ)

    def test_admin_not_installed(self):
        self.assertNotIn("django.contrib.admin", settings.INSTALLED_APPS)

    def test_custom_user_model(self):
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")

    def test_missing_secret_key_fails_clearly(self):
        env = dict(FULL_ENV)
        env.pop("DJANGO_SECRET_KEY")
        result = run_settings_probe(env, "from django.conf import settings; settings.SECRET_KEY")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_missing_database_variable_fails_clearly(self):
        env = dict(FULL_ENV)
        env.pop("DB_PASSWORD")
        result = run_settings_probe(env, "from django.conf import settings; settings.DATABASES")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DB_PASSWORD", result.stderr)

    def test_debug_off_and_localhost_hosts_by_default(self):
        code = "from django.conf import settings; print(settings.DEBUG, settings.ALLOWED_HOSTS)"
        result = run_settings_probe(FULL_ENV, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False ['127.0.0.1', 'localhost']")

    def test_no_secret_key_literal_in_base_settings(self):
        text = (BASE / "config" / "settings" / "base.py").read_text()
        self.assertNotIn("django-insecure", text)


class UserModelTests(TestCase):
    def test_custom_user_is_active_and_has_email(self):
        User = get_user_model()
        self.assertEqual(User._meta.label, "accounts.User")
        user = User.objects.create_user("owner", email="o@example.com", password="pw-Strong-123!")
        self.assertEqual(user.email, "o@example.com")


class MigrationTests(TestCase):
    def test_no_pending_migrations(self):
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
