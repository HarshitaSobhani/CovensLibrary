"""Settings-level guarantees of the two-role model (checked in clean subprocesses)."""

import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE = Path(__file__).resolve().parent.parent
RUNTIME_ENV = {
    "DJANGO_SECRET_KEY": "x" * 60,
    "DB_NAME": "n",
    "DB_USER": "coven_app",
    "DB_PASSWORD": "runtime-pw-123",
}
OWNER_ENV = {"DB_OWNER_USER": "coven_owner", "DB_OWNER_PASSWORD": "owner-pw-456"}


def stub_store_dir(values):
    """A folder with a stand-in ``keyring`` module holding the given runtime secrets (the Windows
    Credential Manager is not available here). Production reads secrets ONLY from the store."""
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="coven-keyring-"))
    items = {f"runtime-{name}": value for name, value in values.items()}
    (folder / "keyring.py").write_text(
        f"VALUES = {items!r}\n\ndef get_password(service, item):\n    return VALUES.get(item)\n"
    )
    return folder


def probe(module, env_extra, code):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_SETTINGS_MODULE": module,
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    env.update(env_extra)
    if module == "config.settings.production":
        # production takes its secrets from the (stand-in) Credential Manager, never the environment
        secrets = {n: env.pop(n) for n in ("DJANGO_SECRET_KEY", "DB_PASSWORD") if n in env}
        env["COVEN_SECRET_STORE"] = "credential-manager"
        env["PYTHONPATH"] = f"{stub_store_dir(secrets)}{os.pathsep}{BASE}"
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BASE, env=env, capture_output=True, text=True
    )


DB = "from django.conf import settings as s; d = s.DATABASES['default']; "


class RuntimeSettingsTests(SimpleTestCase):
    def test_production_uses_runtime_credentials_and_ignores_owner_credentials(self):
        r = probe(
            "config.settings.production",
            {**RUNTIME_ENV, **OWNER_ENV},
            DB + "print(d['USER'], d['PASSWORD'], s.DB_ROLE, s.DB_SECURITY_MODE)",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["coven_app", "runtime-pw-123", "runtime", "strict"])
        leak = probe(
            "config.settings.production",
            {**RUNTIME_ENV, **OWNER_ENV},
            "from django.conf import settings as s; "
            "print('owner-pw-456' in repr({k: getattr(s, k) for k in dir(s) if k.isupper()}))",
        )
        self.assertEqual(leak.stdout.strip(), "False")

    def test_production_requires_runtime_credentials(self):
        for missing in ("DB_USER", "DB_PASSWORD"):
            env = {k: v for k, v in RUNTIME_ENV.items() if k != missing}
            r = probe("config.settings.production", {**env, **OWNER_ENV}, DB + "d['USER']")
            self.assertNotEqual(r.returncode, 0, missing)
            self.assertIn(missing, r.stderr)
            self.assertNotIn("owner-pw-456", r.stderr)

    def test_production_refuses_single_role_dev_mode(self):
        r = probe(
            "config.settings.production",
            {**RUNTIME_ENV, "DB_SECURITY_MODE": "single_role_dev"},
            DB + "d['USER']",
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("strict", r.stderr)

    def test_unknown_security_mode_is_rejected(self):
        r = probe(
            "config.settings.production",
            {**RUNTIME_ENV, "DB_SECURITY_MODE": "off"},
            DB + "d['USER']",
        )
        self.assertNotEqual(r.returncode, 0)

    def test_local_settings_use_the_runtime_role(self):
        r = probe("config.settings.local", {**RUNTIME_ENV}, DB + "print(d['USER'], s.DB_ROLE)")
        self.assertEqual(r.stdout.split(), ["coven_app", "runtime"], r.stderr)


class OwnerSettingsTests(SimpleTestCase):
    def test_migrate_settings_use_owner_credentials_and_know_the_runtime_role_name(self):
        r = probe(
            "config.settings.migrate",
            {**RUNTIME_ENV, **OWNER_ENV},
            DB + "print(d['USER'], d['PASSWORD'], s.DB_ROLE, s.DB_RUNTIME_ROLE)",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["coven_owner", "owner-pw-456", "owner", "coven_app"])

    def test_migrate_settings_require_owner_credentials(self):
        for missing in ("DB_OWNER_USER", "DB_OWNER_PASSWORD"):
            env = {**RUNTIME_ENV, **OWNER_ENV, missing: ""}  # empty beats .env.owner (no override)
            r = probe("config.settings.migrate", env, DB + "d['USER']")
            self.assertNotEqual(r.returncode, 0, missing)
            self.assertIn(missing, r.stderr)

    def test_migrate_settings_refuse_one_role_in_strict_mode(self):
        env = {**RUNTIME_ENV, "DB_OWNER_USER": "coven_app", "DB_OWNER_PASSWORD": "x"}
        r = probe("config.settings.migrate", env, DB + "d['USER']")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("different roles", r.stderr)
        ok = probe(
            "config.settings.migrate",
            {**env, "DB_SECURITY_MODE": "single_role_dev"},
            DB + "print(d['USER'])",
        )
        self.assertEqual(ok.stdout.strip(), "coven_app", ok.stderr)

    def test_test_settings_need_owner_credentials_or_explicit_dev_mode(self):
        r = probe(
            "config.settings.test",
            {**RUNTIME_ENV, "DB_OWNER_USER": "", "DB_OWNER_PASSWORD": ""},
            DB + "d['USER']",
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DB_OWNER_USER", r.stderr)
        ok = probe(
            "config.settings.test",
            {
                **RUNTIME_ENV,
                "DB_OWNER_USER": "",
                "DB_OWNER_PASSWORD": "",
                "DB_SECURITY_MODE": "single_role_dev",
            },
            DB + "print(d['USER'], s.DB_ROLE)",
        )
        self.assertEqual(ok.stdout.split(), ["coven_app", "owner"], ok.stderr)
        ok2 = probe(
            "config.settings.test", {**RUNTIME_ENV, **OWNER_ENV}, DB + "print(d['USER'], s.DB_ROLE)"
        )
        self.assertEqual(ok2.stdout.split(), ["coven_owner", "owner"], ok2.stderr)

    def test_owner_credentials_are_never_in_the_runtime_env_example(self):
        text = (BASE / ".env.example").read_text()
        self.assertNotIn("DB_OWNER_PASSWORD", text)
        self.assertIn("DB_OWNER_PASSWORD", (BASE / ".env.owner.example").read_text())
        self.assertNotIn("DB_OWNER", (BASE / "config" / "settings" / "production.py").read_text())
        self.assertNotIn("DB_OWNER", (BASE / "config" / "wsgi.py").read_text())
