"""One-click launcher support: the server lifecycle (start, duplicate, foreign port, graceful stop,
stale files), the health probe, read-only preflight checks, secret storage, scrubbed logging and the
production-settings guards. Windows-only behaviour (Task Scheduler, services, shortcuts, message
boxes, Credential Manager) is NOT covered here; see docs/DEPLOYMENT.md."""

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from backups import keystore
from core import preflight, runtime
from core.safelog import ScrubFilter, build_logging, scrub_log_text

ROOT = Path(settings.BASE_DIR)
PY = sys.executable


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RuntimeFileTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="coven-run-")

    def test_instance_file_round_trip_and_atomic_write(self):
        self.assertIsNone(runtime.read_instance(self.dir))
        data = runtime.write_instance(8123, self.dir)
        self.assertEqual(runtime.read_instance(self.dir)["port"], 8123)
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["host"], "127.0.0.1")
        self.assertFalse(list(Path(self.dir).glob("*.tmp")))

    def test_only_the_owning_process_clears_its_file(self):
        runtime.write_instance(8123, self.dir)
        runtime.clear_instance(self.dir, pid=os.getpid() + 1)
        self.assertIsNotNone(runtime.read_instance(self.dir))
        runtime.clear_instance(self.dir, pid=os.getpid())
        self.assertIsNone(runtime.read_instance(self.dir))

    def test_a_damaged_instance_file_reads_as_no_instance(self):
        (Path(self.dir) / runtime.INSTANCE_FILE).write_text("{not json")
        self.assertIsNone(runtime.read_instance(self.dir))

    def test_stop_request_protocol(self):
        self.assertFalse(runtime.stop_requested(self.dir))
        runtime.request_stop(self.dir)
        self.assertTrue(runtime.stop_requested(self.dir))
        runtime.clear_stop(self.dir)
        runtime.clear_stop(self.dir)  # clearing twice is fine
        self.assertFalse(runtime.stop_requested(self.dir))

    def test_health_distinguishes_down_foreign_and_ours(self):
        port = free_port()
        self.assertEqual(runtime.health(port), "down")
        with socket.socket() as listener:  # something that is not our application
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            self.assertEqual(runtime.health(port, timeout=1), "foreign")


class HealthEndpointTests(TestCase):
    def test_answers_ok_to_anyone_and_says_nothing_else(self):
        response = self.client.get(reverse("core:healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"app": "coven-library", "status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_reports_unavailable_without_leaking_the_reason(self):
        with mock.patch.object(
            connection, "cursor", side_effect=RuntimeError("password=SECRET host=10.0.0.5")
        ):
            response = self.client.get(reverse("core:healthz"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"app": "coven-library", "status": "unavailable"})
        self.assertNotIn("SECRET", response.content.decode())

    def test_only_reads_are_accepted(self):
        for method in ("post", "put", "delete", "patch"):
            self.assertEqual(getattr(self.client, method)(reverse("core:healthz")).status_code, 405)


class ServerLifecycleTests(SimpleTestCase):
    """Real server processes on a free loopback port (test settings and the test database)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="coven-run-")
        self.port = free_port()
        self.procs = []
        self.addCleanup(self.cleanup)

    def env(self):
        env = {
            **os.environ,
            "COVEN_RUN_DIR": self.dir,
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
        env["DB_NAME"] = connection.settings_dict["NAME"]
        return env

    def start(self, *extra, port=None):
        proc = subprocess.Popen(
            [
                PY,
                "manage.py",
                "serve_local",
                "--no-catchup",
                "--port",
                str(port or self.port),
                *extra,
            ],
            cwd=ROOT,
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.procs.append(proc)
        return proc

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    def wait_healthy(self, port=None, seconds=25):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if runtime.health(port or self.port) == "ok":
                return True
            time.sleep(0.3)
        return False

    def wait_exit(self, proc, seconds=20):
        try:
            return proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None

    def test_start_duplicate_refusal_graceful_stop_and_cleanup(self):
        server = self.start()
        self.assertTrue(self.wait_healthy(), "the server did not become healthy")
        recorded = runtime.read_instance(self.dir)
        self.assertEqual((recorded["pid"], recorded["port"]), (server.pid, self.port))
        # a second copy is refused with its own exit code and leaves the first one alone
        second = self.start()
        self.assertEqual(self.wait_exit(second), 3)
        self.assertEqual(runtime.health(self.port), "ok")
        self.assertEqual(runtime.read_instance(self.dir)["pid"], server.pid)
        # "Stop Library": the server finishes, exits normally and tidies up
        runtime.request_stop(self.dir)
        self.assertEqual(self.wait_exit(server), 0)
        self.assertIsNone(runtime.read_instance(self.dir))
        self.assertFalse(runtime.stop_requested(self.dir))
        self.assertEqual(runtime.health(self.port), "down")

    def test_it_listens_on_loopback_only(self):
        """Read the kernel's own socket table: the listening socket must be bound to 127.0.0.1."""
        table = Path("/proc/net/tcp")
        if not table.exists():
            self.skipTest("needs Linux /proc/net/tcp (the Windows check is in verify_runtime.ps1)")
        server = self.start()
        self.assertTrue(self.wait_healthy())
        listening = []
        for row in table.read_text().splitlines()[1:]:
            fields = row.split()
            address, state = fields[1], fields[3]
            if state == "0A" and int(address.split(":")[1], 16) == self.port:  # 0A = LISTEN
                listening.append(address.split(":")[0])
        self.assertEqual(listening, ["0100007F"])  # 127.0.0.1, and nothing else
        runtime.request_stop(self.dir)
        self.assertEqual(self.wait_exit(server), 0)

    def test_a_port_held_by_another_program_is_never_taken_over(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", self.port))
            other.listen(1)
            server = self.start()
            self.assertEqual(self.wait_exit(server), 4)
            self.assertIsNone(runtime.read_instance(self.dir))

    def test_stale_files_from_a_crashed_run_do_not_block_or_stop_the_next_run(self):
        runtime.write_instance(self.port, self.dir)  # the old server is long gone
        runtime.request_stop(self.dir)  # and its stop request was never consumed
        server = self.start()
        self.assertTrue(self.wait_healthy())
        time.sleep(1.5)  # a stale stop request must not stop the new server
        self.assertEqual(runtime.health(self.port), "ok")
        self.assertEqual(runtime.read_instance(self.dir)["pid"], server.pid)
        runtime.request_stop(self.dir)
        self.assertEqual(self.wait_exit(server), 0)

    def test_it_refuses_to_listen_anywhere_but_loopback(self):
        for host in ("0.0.0.0", "192.168.1.5", "localhost", "::1"):
            done = subprocess.run(
                [PY, "manage.py", "serve_local", "--no-catchup", "--host", host]
                + ["--port", str(self.port)],
                cwd=ROOT,
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(done.returncode, 0, host)
            self.assertIn("127.0.0.1 only", done.stderr, host)
        self.assertEqual(runtime.health(self.port), "down")

    def test_a_terminate_signal_also_ends_it_cleanly(self):
        if os.name == "nt":
            self.skipTest("SIGTERM semantics differ on Windows (the stop file is used there)")
        server = self.start()
        self.assertTrue(self.wait_healthy())
        server.terminate()
        self.assertEqual(self.wait_exit(server), 0)
        self.assertIsNone(runtime.read_instance(self.dir))


class PreflightStageOneTests(SimpleTestCase):
    GOOD = {
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "COVEN_SECRET_STORE": "credential-manager",
        "DB_NAME": "coven_library",
        "DB_USER": "coven_app",
    }

    def run_stage(self, lookup=lambda name: "saved", **changes):
        env = {**self.GOOD, **changes}
        env = {k: v for k, v in env.items() if v is not None}
        return preflight.stage_one(environ=env, secret_lookup=lookup, connect=lambda: None)

    def test_a_complete_configuration_passes(self):
        self.assertIsNone(self.run_stage())

    def test_every_missing_or_unsafe_setting_is_named_and_stops_startup(self):
        for change in (
            {"DJANGO_SETTINGS_MODULE": "config.settings.local"},
            {"DJANGO_SETTINGS_MODULE": None},
            {"DB_NAME": None},
            {"DB_USER": None},
            {"DB_USER": "coven_owner"},
            {"DB_OWNER_PASSWORD": "pw"},
            {"DB_OWNER_USER": "coven_owner"},
            {"DJANGO_DEBUG": "true"},
            {"COVEN_SECRET_STORE": None},
            {"COVEN_SECRET_STORE": "environment"},
        ):
            problem = self.run_stage(**change)
            self.assertIsNotNone(problem, change)
            self.assertEqual(problem.code, "CONFIG", change)
            self.assertNotIn("pw", problem.detail.replace("password", ""))

    def test_a_plaintext_secret_in_the_environment_is_refused_never_used(self):
        for name in ("DJANGO_SECRET_KEY", "DB_PASSWORD", "EMAIL_HOST_PASSWORD"):
            problem = self.run_stage(**{name: "plain-text-value"})
            self.assertEqual(problem.code, "CONFIG", name)
            self.assertIn(name, problem.detail)
            self.assertNotIn("plain-text-value", problem.detail)

    def test_a_missing_secret_stops_startup(self):
        for missing in ("DJANGO_SECRET_KEY", "DB_PASSWORD"):
            problem = self.run_stage(lookup=lambda name, m=missing: None if name == m else "ok")
            self.assertEqual(problem.code, "SECRETS", missing)

    def test_an_unreadable_credential_store_fails_closed_and_is_told_apart(self):
        def broken(name):
            raise RuntimeError("locked")

        problem = self.run_stage(lookup=broken)
        self.assertEqual((problem.code, problem.exit_code), ("STORE_UNAVAILABLE", 30))
        self.assertNotIn("locked", problem.line().replace("RuntimeError", ""))

    def test_database_down_is_reported_as_such(self):
        def refuse():
            raise ConnectionRefusedError()

        env = {**self.GOOD}
        problem = preflight.stage_one(environ=env, secret_lookup=lambda n: "x", connect=refuse)
        self.assertEqual((problem.code, problem.exit_code), ("DB_DOWN", 22))

    def test_owner_text_is_plain_and_the_detail_is_the_technical_part(self):
        for code, text in preflight.OWNER_TEXT.items():
            self.assertNotRegex(text, r"[A-Za-z]+Error|Traceback|manage\.py|\.py\b", code)
        line = json.loads(preflight.Problem("MIGRATIONS", "3 migration(s)").line())
        self.assertEqual(line["ok"], False)
        self.assertEqual(set(line), {"ok", "code", "owner", "detail"})
        self.assertEqual(len(set(preflight.CODES.values())), len(preflight.CODES))
        self.assertNotIn(0, preflight.CODES.values())


class PreflightStageTwoTests(TestCase):
    def run_check(self, **overrides):
        from core.management.commands.launcher_preflight import Command

        return Command().check()

    def setUp(self):
        keystore.reset_memory_keystore()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_a_healthy_setup_passes_and_nothing_is_created(self):
        keystore.save_working_key(keystore.generate_key())
        self.assertIsNone(self.run_check())

    def test_a_missing_key_stops_startup_and_is_not_created(self):
        problem = self.run_check()
        self.assertEqual(problem.code, "KEY_MISSING")
        self.assertIsNone(keystore.load_working_key())  # a check never makes a key

    def test_a_damaged_key_is_reported_as_unavailable(self):
        keystore.get_keystore().set(keystore.WORKING_KEY, "not-a-key")
        self.assertEqual(self.run_check().code, "KEY_UNAVAILABLE")

    def test_an_unreadable_store_is_reported_as_unavailable(self):
        with mock.patch.object(keystore, "get_keystore") as store:
            store.return_value.get.side_effect = keystore.KeyStoreError("locked")
            self.assertEqual(self.run_check().code, "KEY_UNAVAILABLE")

    def test_unapplied_migrations_stop_startup_and_are_not_applied(self):
        keystore.save_working_key(keystore.generate_key())
        from django.db.migrations.executor import MigrationExecutor

        fake_plan = [(mock.Mock(), False)]
        with mock.patch.object(MigrationExecutor, "migration_plan", return_value=fake_plan):
            self.assertEqual(self.run_check().code, "MIGRATIONS")

    def test_a_database_that_cannot_be_reached_is_named(self):
        from django.db.utils import OperationalError

        with mock.patch.object(
            connection, "ensure_connection", side_effect=OperationalError("nope")
        ):
            self.assertEqual(self.run_check().code, "DB_DOWN")
        with mock.patch.object(
            connection,
            "ensure_connection",
            side_effect=OperationalError("password authentication failed"),
        ):
            self.assertEqual(self.run_check().code, "DB_AUTH")

    def test_database_security_drift_stops_startup(self):
        keystore.save_working_key(keystore.generate_key())
        from core import dbsecurity

        finding = (
            dbsecurity.Finding("error", "coven.E001", "drift")
            if hasattr(dbsecurity, "Finding")
            else None
        )
        if finding is None:
            self.skipTest("Finding type not available")
        with mock.patch.object(dbsecurity, "inspect", return_value=[finding]):
            self.assertEqual(self.run_check().code, "DB_SECURITY")

    def test_missing_display_files_stop_startup(self):
        keystore.save_working_key(keystore.generate_key())
        with override_settings(WHITENOISE_USE_FINDERS=False, STATIC_ROOT=Path(tempfile.mkdtemp())):
            self.assertEqual(self.run_check().code, "STATIC")

    def test_the_command_prints_one_json_line_and_exits_with_the_code(self):
        from django.core.management import call_command

        out = tempfile.TemporaryFile("w+")
        with self.assertRaises(SystemExit) as ctx:
            call_command("launcher_preflight", stdout=out)
        self.assertEqual(ctx.exception.code, preflight.CODES["KEY_MISSING"])
        out.seek(0)
        self.assertEqual(json.loads(out.read())["code"], "KEY_MISSING")


class StoreSecretsTests(SimpleTestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "deploy" / "windows"))
        self.addCleanup(sys.path.remove, str(ROOT / "deploy" / "windows"))
        import store_secrets

        from config import secretstore

        self.mod = store_secrets
        self.secretstore = secretstore

        class Store:
            NAMES = secretstore.NAMES
            SecretStoreError = secretstore.SecretStoreError

            def __init__(self):
                self.items = {}
                self.broken = False

            def get_checked(self, name):
                if self.broken:
                    raise secretstore.SecretStoreError("the store could not be read")
                return self.items.get(name)

            def put(self, name, value, replace=False):
                if self.items.get(name) and not replace:
                    raise secretstore.SecretStoreError(f"{name} is already saved")
                self.items[name] = value

        self.store = Store()
        self.said = []

    def answers(self, *values):
        queue = list(values)
        return lambda prompt: queue.pop(0)

    def test_first_run_generates_the_key_and_asks_for_the_database_password_twice(self):
        saved, kept = self.mod.run(
            ask=self.answers("Real-Db-Pass-1!", "Real-Db-Pass-1!", ""),
            store=self.store,
            out=self.said.append,
        )
        self.assertEqual(saved, ["DJANGO_SECRET_KEY", "DB_PASSWORD"])
        self.assertGreaterEqual(len(self.store.items["DJANGO_SECRET_KEY"]), 60)
        self.assertEqual(self.store.items["DB_PASSWORD"], "Real-Db-Pass-1!")
        self.assertNotIn("EMAIL_HOST_PASSWORD", self.store.items)
        for line in self.said:  # nothing secret is ever printed
            self.assertNotIn("Real-Db-Pass-1!", line)
            self.assertNotIn(self.store.items["DJANGO_SECRET_KEY"], line)

    def test_existing_secrets_are_never_replaced_without_an_explicit_request(self):
        self.store.items.update(DJANGO_SECRET_KEY="keep-me", DB_PASSWORD="keep-too")
        saved, kept = self.mod.run(ask=self.answers(""), store=self.store, out=self.said.append)
        self.assertEqual(saved, [])
        self.assertEqual(self.store.items["DJANGO_SECRET_KEY"], "keep-me")
        self.assertEqual(self.store.items["DB_PASSWORD"], "keep-too")
        self.mod.run(
            replace=("DB_PASSWORD",),
            ask=self.answers("New-Pass-2!", "New-Pass-2!", ""),
            store=self.store,
            out=self.said.append,
        )
        self.assertEqual(self.store.items["DB_PASSWORD"], "New-Pass-2!")
        self.assertEqual(self.store.items["DJANGO_SECRET_KEY"], "keep-me")

    def test_an_unreadable_store_stops_everything_and_saves_nothing(self):
        self.store.broken = True
        with self.assertRaises(SystemExit) as ctx:
            self.mod.run(ask=self.answers("x", "x"), store=self.store, out=print)
        self.assertIn("Nothing was saved", str(ctx.exception))
        self.assertEqual(self.store.items, {})

    def test_a_mismatch_or_placeholder_saves_nothing(self):
        with self.assertRaises(SystemExit):
            self.mod.run(ask=self.answers("a-pass-1", "a-pass-2"), store=self.store, out=print)
        with self.assertRaises(SystemExit):
            self.mod.run(ask=self.answers("change-me"), store=self.store, out=print)
        self.assertNotIn("DB_PASSWORD", self.store.items)

    def test_the_real_store_module_never_replaces_silently_and_fails_closed(self):
        stub = mock.MagicMock()
        values = {}
        stub.get_password.side_effect = lambda service, item: values.get(item)
        stub.set_password.side_effect = lambda service, item, value: values.__setitem__(item, value)
        with mock.patch.dict(sys.modules, {"keyring": stub}):
            self.secretstore.put("DB_PASSWORD", "first")
            with self.assertRaises(self.secretstore.SecretStoreError):
                self.secretstore.put("DB_PASSWORD", "second")  # an existing secret stays
            self.assertEqual(self.secretstore.get_checked("DB_PASSWORD"), "first")
            self.secretstore.put("DB_PASSWORD", "second", replace=True)
            self.assertEqual(self.secretstore.get_checked("DB_PASSWORD"), "second")
            with self.assertRaises(self.secretstore.SecretStoreError):
                self.secretstore.put("DB_PASSWORD", "")  # never an empty secret
            stub.get_password.side_effect = RuntimeError("backend locked")
            with self.assertRaises(self.secretstore.SecretStoreError) as ctx:
                self.secretstore.get_checked("DB_PASSWORD")  # broken store: an error, not None
            self.assertNotIn("locked", str(ctx.exception))
            self.assertIsNone(self.secretstore.get("DB_PASSWORD"))  # the display helper only

    def test_the_account_probe_round_trips_a_throwaway_item_and_never_touches_a_secret(self):
        stub = mock.MagicMock()
        values = {}
        stub.set_password.side_effect = lambda service, item, value: values.__setitem__(item, value)
        stub.get_password.side_effect = lambda service, item: values.get(item)
        stub.delete_password.side_effect = lambda service, item: values.pop(item, None)
        values["runtime-DB_PASSWORD"] = "real-secret"
        with mock.patch.dict(sys.modules, {"keyring": stub}):
            self.secretstore.probe()
            self.assertEqual(
                values, {"runtime-DB_PASSWORD": "real-secret"}
            )  # nothing left or changed
            touched = {c.args[1] for c in stub.set_password.call_args_list}
            self.assertEqual(touched, {self.secretstore.PROBE_ITEM})
            stub.get_password.side_effect = lambda service, item: "something else"
            with self.assertRaises(self.secretstore.SecretStoreError):
                self.secretstore.probe()  # a store that does not return what was written
            stub.set_password.side_effect = OSError("no credential manager")
            with self.assertRaises(self.secretstore.SecretStoreError) as ctx:
                self.secretstore.probe()
            self.assertNotIn("no credential manager", str(ctx.exception))

    def test_the_check_option_reports_success_or_stops_without_saving_anything(self):
        values = {}
        stub = mock.MagicMock()
        stub.set_password.side_effect = lambda service, item, value: values.__setitem__(item, value)
        stub.get_password.side_effect = lambda service, item: values.get(item)
        stub.delete_password.side_effect = lambda service, item: values.pop(item, None)
        with mock.patch.dict(sys.modules, {"keyring": stub}):
            self.mod.main(["--check"])
            self.assertEqual(values, {})
            stub.set_password.side_effect = OSError("locked")
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main(["--check"])
            self.assertIn("FAILED", str(ctx.exception))

    def test_the_secret_store_only_knows_its_three_names(self):
        self.assertIsNone(self.secretstore.get("SOMETHING_ELSE"))
        with self.assertRaises(ValueError):
            self.secretstore.put("SOMETHING_ELSE", "x")
        with self.assertRaises(self.secretstore.SecretStoreError):
            self.secretstore.get_checked("SOMETHING_ELSE")


class ProductionSettingsTests(SimpleTestCase):
    """The production settings are loaded in a fresh process with a stub ``keyring`` (the Windows
    Credential Manager is not available here)."""

    BASE = {
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        "COVEN_SECRET_STORE": "credential-manager",
        "DB_NAME": "coven_library",
        "DB_USER": "coven_app",
    }
    STORE = {"runtime-DJANGO_SECRET_KEY": "k" * 50, "runtime-DB_PASSWORD": "stored-pw"}

    def stub(self, values=None, body=None):
        folder = Path(tempfile.mkdtemp())
        content = body or (
            f"VALUES = {dict(self.STORE if values is None else values)!r}\n"
            "def get_password(service, item):\n"
            "    return VALUES.get(item)\n"
        )
        (folder / "keyring.py").write_text(content)
        return folder

    def load(self, folder, drop=(), **extra):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DB_", "DJANGO_", "COVEN_", "EMAIL_"))
        }
        env.update(self.BASE)
        env.update(extra)
        env["PYTHONPATH"] = f"{folder}{os.pathsep}{ROOT}"
        for name in drop:
            env.pop(name, None)
        code = (
            "import django.conf as c; s = c.settings; "
            "print(s.DEBUG, bool(s.SECRET_KEY), s.DATABASES['default']['PASSWORD'])"
        )
        return subprocess.run(
            [PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
        )

    def test_secrets_come_from_the_credential_manager(self):
        done = self.load(self.stub())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["False", "True", "stored-pw"])

    def test_the_store_must_be_declared_or_production_refuses_to_load(self):
        done = self.load(self.stub(), drop=("COVEN_SECRET_STORE",))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("COVEN_SECRET_STORE=credential-manager", done.stderr)

    def test_a_plaintext_environment_secret_is_refused_not_used_as_a_fallback(self):
        for name in ("DJANGO_SECRET_KEY", "DB_PASSWORD", "EMAIL_HOST_PASSWORD"):
            done = self.load(self.stub(), **{name: "plain-text"})
            self.assertNotEqual(done.returncode, 0, name)
            self.assertIn(f"{name} is set as an environment variable", done.stderr)
            self.assertNotIn("plain-text", done.stderr)

    def test_env_secrets_are_ignored_even_when_the_store_is_empty(self):
        done = self.load(self.stub(values={}), DB_PASSWORD="plain-text", DJANGO_SECRET_KEY="k" * 50)
        self.assertNotEqual(done.returncode, 0)  # refused, never silently used

    def test_a_missing_secret_stops_startup_with_a_clear_message(self):
        for missing in ("runtime-DJANGO_SECRET_KEY", "runtime-DB_PASSWORD"):
            values = {k: v for k, v in self.STORE.items() if k != missing}
            done = self.load(self.stub(values=values))
            self.assertNotEqual(done.returncode, 0, missing)
            self.assertIn("is not saved in the Windows Credential Manager", done.stderr)

    def test_an_unreadable_store_fails_closed(self):
        body = "def get_password(service, item):\n    raise OSError('credential store locked')\n"
        done = self.load(self.stub(body=body))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("could not be read", done.stderr)
        self.assertIn("will not start", done.stderr)

    def test_no_keyring_at_all_fails_closed(self):
        body = "raise ImportError('no backend')\n"
        done = self.load(self.stub(body=body))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("could not be read", done.stderr)

    def test_debug_is_refused_in_production(self):
        done = self.load(self.stub(), DJANGO_DEBUG="true")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("DJANGO_DEBUG must not be enabled", done.stderr)

    def test_development_settings_still_read_the_environment(self):
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("DB_", "DJANGO_", "COVEN_"))
        }
        env.update(DJANGO_SETTINGS_MODULE="config.settings.base", DJANGO_SECRET_KEY="dev-key")
        done = subprocess.run(
            [PY, "-c", "import django.conf as c; print(c.settings.SECRET_KEY)"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        self.assertEqual(done.stdout.strip(), "dev-key", done.stderr)

    def test_the_csrf_cookie_is_a_session_cookie(self):
        self.assertIsNone(settings.CSRF_COOKIE_AGE)


class ScrubbedLoggingTests(SimpleTestCase):
    def record(self, msg, *args, exc=None):
        return logging.LogRecord("django.request", logging.ERROR, __file__, 1, msg, args, exc)

    def scrubbed(self, rec):
        ScrubFilter().filter(rec)
        return rec.getMessage() + (rec.exc_text or "")

    def test_personal_data_and_credentials_never_reach_a_log(self):
        samples = {
            "Key (mobile_e164)=(+919876543210) already exists.": "919876543210",
            "send failed for asha.rao@example.com": "asha.rao@example.com",
            "call +91 98765 43210 now": "98765",
            "DETAIL: Failing row contains (5, Asha Rao, 9876543210)": "Asha Rao",
            "password=Hunter2-Secret and token=abcdef1234567890abcdef": "Hunter2",
            "GET /accounts/reset/MQ/cx9-" + "0123456789abcdef" * 2 + "/": "0123456789abcdef" * 2,
            "Not Found: /search/?q=Asha+Rao+9876543210": "Asha",
            "Authorization: Bearer ya29.abcdefghijklmnopqrstuvwxyz": "ya29.abcdef",
        }
        for text, secret in samples.items():
            out = self.scrubbed(self.record(text))
            self.assertNotIn(secret, out, text)

    def test_exception_text_is_scrubbed_too(self):
        try:
            raise ValueError("Key (email)=(owner@example.com) already exists")
        except ValueError:
            import sys as _sys

            rec = self.record("boom", exc=_sys.exc_info())
        out = self.scrubbed(rec)
        self.assertNotIn("owner@example.com", out)
        self.assertIn("ValueError", out)  # the useful part stays

    def test_ordinary_diagnostics_survive(self):
        out = self.scrubbed(self.record("Not Found: %s", "/members/12/"))
        self.assertEqual(out, "Not Found: /members/12/")
        out = self.scrubbed(self.record("database connection failed (OperationalError)"))
        self.assertIn("OperationalError", out)

    def test_a_broken_format_string_does_not_break_logging(self):
        rec = self.record("bad %s %s", "only-one")
        self.assertTrue(ScrubFilter().filter(rec))

    def test_the_log_file_is_created_with_rotation_and_the_filter(self):
        folder = Path(tempfile.mkdtemp()) / "logs"
        config = build_logging(str(folder))
        self.assertTrue(folder.is_dir())
        handler = config["handlers"]["file"]
        self.assertIn("RotatingFileHandler", handler["class"])
        self.assertEqual(handler["filters"], ["scrub"])
        self.assertEqual(config["loggers"]["django"]["handlers"], ["file"])
        console = build_logging("")
        self.assertEqual(console["loggers"]["django"]["handlers"], ["console"])
        self.assertEqual(console["handlers"]["console"]["filters"], ["scrub"])

    def test_end_to_end_through_a_configured_logger(self):
        import logging.config

        folder = Path(tempfile.mkdtemp())
        logging.config.dictConfig(build_logging(str(folder)))
        try:
            logging.getLogger("django.request").error(
                "Key (mobile_e164)=(+919876543210) already exists for owner@example.com"
            )
            for handler in logging.getLogger("django").handlers:
                handler.flush()
            text = (folder / "application.log").read_text()
            self.assertNotIn("9876543210", text)
            self.assertNotIn("owner@example.com", text)
            self.assertIn("already exists", text)
        finally:
            logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})

    def test_scrub_function_is_idempotent(self):
        once = scrub_log_text("mail a@b.co or +919876543210")
        self.assertEqual(scrub_log_text(once), once)
