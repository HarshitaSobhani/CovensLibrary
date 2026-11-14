"""Static checks of the Windows launcher scripts and the production lock file. PowerShell cannot be
run here (Linux/macOS CI): these tests read the scripts and enforce the rules the owner set, so a
careless edit cannot, for example, make a normal start run migrations or open the network.
The scripts themselves are UNVERIFIED on Windows."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
WIN = ROOT / "deploy" / "windows"


def text(name):
    return (WIN / name).read_text(encoding="utf-8")


def code_lines(name):
    return [
        line
        for line in text(name).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class LauncherScriptRules(SimpleTestCase):
    START = "Start-CovensLibrary.ps1"

    def test_a_normal_start_never_does_anything_destructive_or_one_time(self):
        body = "\n".join(code_lines(self.START))
        for forbidden in (
            r"\bmigrate\b",
            r"\bmakemigrations\b",
            r"setup_backup_key",
            r"show_recovery_key",
            r"restore_backup",
            r"create_owner",
            r"apply_db_grants",
            r"issue_recovery_code",
            r"pip\s+install",
            r"\bpip\b.*\binstall\b",
            r"git\s+(pull|fetch|checkout)",
            r"Remove-Item\s+-Recurse",
            r"runserver",
            r"DROP\s|TRUNCATE\s",
            r"--replace",
            r"setx\b",
            r"\[Environment\]::SetEnvironmentVariable\([^)]*\"(User|Machine)\"",
            r"Set-ExecutionPolicy",
            r"RunAs",
            r"-Verb\s+runas",
        ):
            self.assertIsNone(re.search(forbidden, body, re.I), forbidden)

    def test_it_uses_the_existing_production_server_on_loopback_only(self):
        body = text(self.START)
        self.assertIn('"serve_local"', body)
        self.assertIn('"--host", $HostName', body)
        self.assertEqual(
            re.findall(r'^\$HostName = "([^"]+)"', text("common.ps1"), re.M), ["127.0.0.1"]
        )
        for name in (
            "common.ps1",
            self.START,
            "Stop-CovensLibrary.ps1",
            "verify_runtime.ps1",
            "start_app.ps1",
        ):
            self.assertNotRegex(
                text(name), r"0\.0\.0\.0|\+:\d+|IPAddress\]::Any|--host\s+(?!\$HostName)\S+"
            )

    def test_the_browser_opens_only_after_the_health_check_and_never_in_silent_mode(self):
        body = text(self.START)
        self.assertLess(
            body.index('(Get-AppHealth) -eq "ok"'), body.index("Open-Browser\n    Start-Sleep")
        )
        self.assertRegex(
            body, r"function Open-Browser \{ if \(-not \$Silent -and -not \$NoBrowser\)"
        )
        self.assertEqual(body.count('Start-Process "http://'), 1)

    def test_it_refuses_a_second_copy_and_a_foreign_program_on_the_port(self):
        body = text(self.START)
        for state in ('"ok"', '"unhealthy"', '"foreign"'):
            self.assertIn(f"$state -eq {state}", body)
        self.assertIn("serve_local", body)

    def test_failures_show_plain_language_and_details_go_to_the_log_only(self):
        body = text(self.START)
        self.assertIn("Could not start the application. Contact support.", body)
        self.assertIn("Starting Coven's Library...", body)
        self.assertIn("Checking the database...", body)
        self.assertIn("Starting the application...", body)
        self.assertIn("Coven's Library is ready.", body)
        message_box = re.search(r"MessageBox\]::Show\((.*)\)", body).group(1)
        self.assertNotIn("$Technical", message_box)
        self.assertNotIn("Exception", message_box)
        self.assertIn('Write-Log "FAILED: $Text | $Technical"', body)

    def test_the_stop_script_ends_only_the_recorded_process_and_only_as_a_last_resort(self):
        body = text("Stop-CovensLibrary.ps1")
        self.assertIn("stop.request", body)
        self.assertLess(body.index("stop.request"), body.index("Stop-Process"))
        self.assertEqual(body.count("Stop-Process"), 1)
        self.assertNotRegex(
            body, r"Stop-Process\s+-Name|taskkill|Get-Process\s+(-Name\s+)?python|killall|pkill"
        )
        for proof in ("$instance.pid", "serve_local", "StartTime"):
            self.assertIn(proof, body)

    def test_no_script_contains_a_secret_or_a_password_value(self):
        for path in list(WIN.glob("*.ps1")) + [WIN / "RunHidden.vbs", WIN / "store_secrets.py"]:
            body = path.read_text(encoding="utf-8")
            self.assertNotRegex(
                body, r"(?i)(password|secret|token|apikey)\s*=\s*[\"'][^\"'\s$]{4,}", path.name
            )
            self.assertNotRegex(body, r"AKIA[0-9A-Z]{16}|-----BEGIN|ya29\.", path.name)

    def test_secrets_are_not_accepted_in_the_configuration_file(self):
        body = text("common.ps1")
        allowed = re.search(r"\$AllowedConfigKeys = @\((.*?)\)", body, re.S).group(1)
        self.assertNotRegex(allowed, r"(?i)password|secret|key\b(?!_)")
        self.assertNotIn("DB_PASSWORD", allowed)
        self.assertNotIn("EMAIL_HOST_PASSWORD", allowed)
        self.assertIn('COVEN_SECRET_STORE = "credential-manager"', body)
        self.assertIn('DJANGO_DEBUG = "false"', body)

    def test_folders_are_private_to_the_owner_account(self):
        body = text("common.ps1")
        self.assertIn("/inheritance:r", body)
        self.assertIn("/grant:r", body)
        self.assertNotRegex(
            body, r"Everyone|Users:|BUILTIN\\Users|\(OI\)\(CI\)(R|M|F)\s*\"?\s*,\s*\"?\*S-1-1-0"
        )

    def test_the_hidden_launcher_can_start_only_the_two_known_scripts(self):
        body = text("RunHidden.vbs")
        self.assertEqual(
            sorted(re.findall(r'"([A-Za-z-]+\.ps1)"', body)),
            ["Start-CovensLibrary.ps1", "Stop-CovensLibrary.ps1"],
        )
        self.assertIn("WScript.Quit 2", body)

    def test_the_logon_application_task_is_optional_and_the_jobs_task_is_always_registered(self):
        body = text("register_tasks.ps1")
        self.assertIn("[switch]$AutoStartApplication", body)
        self.assertRegex(
            body,
            r"if \(\$AutoStartApplication\) \{\s*Register-ScheduledTask -TaskName \$names\[0\]",
        )
        self.assertRegex(body, r"Register-ScheduledTask -TaskName \$names\[1\]")
        self.assertIn("-RunLevel Limited", body)  # never elevated

    def test_setup_does_not_create_roles_run_migrations_or_touch_the_backup_key(self):
        body = "\n".join(code_lines("Setup-CovensLibrary.ps1"))
        for forbidden in (
            r"\bmigrate\b",
            r"setup_backup_key",
            r"restore_backup",
            r"provision_roles",
            r"CREATE ROLE",
            r"DROP ",
        ):
            self.assertIsNone(re.search(forbidden, body, re.I), forbidden)
        self.assertIn("--require-hashes", body)
        self.assertIn("store_secrets.py", body)

    def test_the_owner_password_is_never_written_down_by_the_helper(self):
        body = "\n".join(code_lines("Run-AsOwner.ps1"))
        self.assertIn("-AsSecureString", body)
        self.assertIn("ZeroFreeBSTR", body)
        self.assertIn("Remove-Item Env:\\DB_OWNER_PASSWORD", body)
        self.assertNotRegex(
            body, r"Set-Content|Out-File|Add-Content|\.env\.owner|Write-(Host|Output|Log).*PASSWORD"
        )
        self.assertIn("--settings=config.settings.migrate", body)
        # the launcher itself must never see owner credentials
        self.assertNotIn("DB_OWNER", text("Start-CovensLibrary.ps1"))

    def test_the_daily_launcher_never_installs_packages(self):
        for name in (
            "Start-CovensLibrary.ps1",
            "Stop-CovensLibrary.ps1",
            "start_app.ps1",
            "run_jobs.ps1",
        ):
            self.assertNotRegex(
                "\n".join(code_lines(name)), r"\bpip\b|Install-Package|winget|choco", name
            )


def function_body(script, name):
    body = text(script)
    start = body.index(f"function {name}")
    nxt = re.search(r"\nfunction ", body[start + 10 :])
    return body[start : start + 10 + nxt.start()] if nxt else body[start:]


class BitLockerRules(SimpleTestCase):
    """Decision S-05: BitLocker is mandatory before production data. The scripts only READ its
    status; they never enable it, never touch recovery settings and never show a recovery key."""

    SCRIPTS = list(WIN.glob("*.ps1"))

    def test_no_script_changes_bitlocker_or_handles_a_recovery_key(self):
        forbidden = (
            r"Enable-BitLocker",
            r"Disable-BitLocker",
            r"Suspend-BitLocker",
            r"Resume-BitLocker",
            r"Lock-BitLocker",
            r"Unlock-BitLocker",
            r"Add-BitLockerKeyProtector",
            r"Remove-BitLockerKeyProtector",
            r"Backup-BitLockerKeyProtector",
            r"manage-bde",
            r"\.RecoveryPassword\b",
            r"\bKeyProtectorId\b",
            r"BackupToAAD|BackupToMicrosoft",
            r"Out-GridView",
        )
        self.assertGreaterEqual(len(self.SCRIPTS), 12)
        for path in self.SCRIPTS:
            body = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(re.search(pattern, body, re.I), (path.name, pattern))

    def test_only_the_number_of_recovery_protectors_is_read_and_never_printed_as_a_key(self):
        body = function_body("bitlocker.ps1", "Test-BitLockerProtection")
        self.assertIn("KeyProtectorType", body)
        self.assertIn(".Count", body)
        self.assertEqual(body.count("KeyProtector"), 2)  # the pipeline and the type filter
        self.assertNotRegex(body, r"Write-(Host|Output|Log)[^\n]*\$v\b")

    def test_only_a_confirmed_and_complete_protection_passes(self):
        body = function_body("bitlocker.ps1", "Test-BitLockerProtection")
        self.assertEqual(len(re.findall(r'State = "Protected"', body)), 1)
        self.assertRegex(body, r"EncryptionInProgress[^\n]*InProgress")
        for state in ("Off", "Unavailable", "NeedsAdministrator", "Unknown", "InProgress"):
            self.assertIn(f'State = "{state}"', body)
        self.assertIn("-ge 1", body)  # at least one recovery protector must exist
        self.assertIn('-eq "FullyEncrypted"', body)  # FULL encryption, not a partial state
        self.assertNotIn("UsedSpaceOnly", body)

    def test_the_helper_file_has_no_side_effects_when_loaded(self):
        body = "\n".join(code_lines("bitlocker.ps1"))
        top_level = [
            line for line in body.splitlines() if line and not line.startswith((" ", "\t", "}"))
        ]
        for line in top_level:
            self.assertRegex(line, r"^(\$\w+ = Join-Path|function \w|\})", line)
        for forbidden in (
            r"Initialize-DataDir",
            r"Write-Log",
            r"New-Item",
            r"Set-Content",
            r"icacls",
            r"LOCALAPPDATA",
            r"keyring|Credential",
        ):
            self.assertIsNone(re.search(forbidden, body, re.I), forbidden)

    def test_the_owner_is_told_what_to_do_in_plain_words(self):
        action = function_body("bitlocker.ps1", "Get-BitLockerOwnerAction")
        for needle in (
            "Device encryption",
            "BitLocker drive encryption",
            "Wait until",
            "Windows 11 Pro",
            "Verify-BitLocker-Admin.ps1",
            "OFF this computer",
        ):
            self.assertIn(needle, action)

    def test_verification_treats_a_missing_bitlocker_as_a_failure(self):
        body = text("verify_runtime.ps1")
        self.assertRegex(body, r'Check "An administrator verified BitLocker')
        self.assertRegex(body, r'Check "The owner confirmed the BitLocker recovery key')
        self.assertNotRegex(body, r'Info "Disk encryption')

    def test_a_windows_parser_check_is_provided_and_reads_only(self):
        body = text("Test-Syntax.ps1")
        self.assertIn("System.Management.Automation.Language.Parser", body)
        self.assertIn("ParseFile", body)
        self.assertIn("*.ps1", body)  # includes bitlocker.ps1 and the two administrator scripts
        self.assertNotRegex(body, r"Invoke-Expression|iex\b|&\s*\$|\. \$|Start-Process")


class TwoAccountInstallationRules(SimpleTestCase):
    """Fix 3: the administrator's BitLocker step and the owner's setup are separate accounts and
    separate processes. Nothing of the owner's is ever created in the administrator's profile, and
    the owner's setup is never elevated."""

    ADMIN = "Verify-BitLocker-Admin.ps1"
    GRANT = "Grant-DatabaseServiceControl-Admin.ps1"

    def test_the_administrator_step_requires_elevation_and_does_nothing_else(self):
        body = text(self.ADMIN)
        code = "\n".join(code_lines(self.ADMIN))
        self.assertIn("Test-IsElevated", code)
        self.assertRegex(code, r"if \(-not \(Test-IsElevated\)\) \{[^}]*exit 1")
        self.assertIn("bitlocker.ps1", code)
        # it never loads the application's helpers (which know the owner's profile and secrets)
        self.assertNotIn("common.ps1", code)
        for forbidden in (
            r"Initialize-DataDir",
            r"Write-Log",
            r"LOCALAPPDATA",
            r"USERPROFILE",
            r"\$DataDir|\$ConfigDir|\$LogDir|\$RunDir",
            r"keyring|cmdkey|CredRead|CredWrite|Get-StoredCredential|store_secrets|secretstore",
            r"app\.env|\.venv|manage\.py",
            r"New-Shortcut|CreateShortcut|\.lnk",
            r"Register-ScheduledTask",
            r"\[Environment\]::SetEnvironmentVariable|setx",
        ):
            self.assertIsNone(re.search(forbidden, code, re.I), forbidden)
        header = body[: body.index("\n. (Join-Path")]
        for promise in (
            "never enables BitLocker",
            "never displays, exports, copies or logs a recovery key",
            "writes NOTHING in the administrator's profile",
        ):
            self.assertIn(promise, header)

    def test_the_administrator_step_writes_only_the_machine_wide_record(self):
        code = "\n".join(code_lines(self.ADMIN))
        writes = [
            m.group(0)
            for m in re.finditer(r"(?:Set-Content|Add-Content|Out-File|New-Item)[^\n]*", code)
        ]
        self.assertEqual(len(writes), 2, writes)
        self.assertTrue(any("$MachineAttestationDir" in w for w in writes))
        self.assertTrue(any("$MachineAttestationFile" in w for w in writes))
        self.assertIn("ProgramData", text("bitlocker.ps1"))
        # the record holds no secret and no key
        record = code[code.index("$record = [ordered]@{") :]
        record = record[: record.index("}")]
        for field in (
            "schema",
            "checked_utc",
            "computer",
            "drive",
            "protection_status",
            "volume_status",
            "recovery_protectors",
            "verified_by_administrator",
        ):
            self.assertIn(field, record)
        self.assertNotRegex(record, r"(?i)password|key\b|secret|token")

    def test_the_machine_record_is_writable_only_by_administrators(self):
        code = "\n".join(code_lines(self.ADMIN))
        self.assertIn("/inheritance:r", code)
        self.assertIn("*S-1-5-18:(OI)(CI)F", code)  # SYSTEM
        self.assertIn("*S-1-5-32-544:(OI)(CI)F", code)  # Administrators
        self.assertIn("*S-1-5-32-545:(OI)(CI)RX", code)  # Users: read and execute only
        self.assertNotRegex(code, r"Everyone|S-1-1-0|S-1-5-32-545:\(OI\)\(CI\)(F|M|W)")

    def test_the_administrator_step_explains_the_owner_confirmation_requirement(self):
        body = text(self.ADMIN)
        for needle in (
            "OFF this computer",
            "owner",
            "NOT as administrator",
            "never shows that key",
            "Setup-CovensLibrary.ps1",
        ):
            self.assertIn(needle, body)

    def test_owner_setup_refuses_to_run_elevated_or_as_an_unconfirmed_account(self):
        code = "\n".join(code_lines("Setup-CovensLibrary.ps1"))
        self.assertRegex(code, r"if \(Test-IsElevated\) \{\s*Fail ")
        self.assertIn('Read-Host "Type the Windows account name', code)
        self.assertRegex(code, r"Fail \"That is not the account you are signed in as")
        self.assertIn("LOCALAPPDATA.StartsWith($env:USERPROFILE", code)
        order = [
            code.index(marker)
            for marker in (
                "Test-IsElevated",
                'Read-Host "Type the Windows account name',
                "Assert-BitLockerAttestation",
                "-m venv",
                "--require-hashes",
                'store_secrets.py") --check',
                'store_secrets.py")\n',
                "New-Shortcuts.ps1",
                "Start-CovensLibrary.ps1",
            )
        ]
        self.assertEqual(order, sorted(order))  # the account is settled before anything is created

    def test_owner_setup_never_elevates_switches_accounts_or_uses_plaintext_secrets(self):
        for name in (
            "Setup-CovensLibrary.ps1",
            "New-Shortcuts.ps1",
            "register_tasks.ps1",
            "common.ps1",
        ):
            code = "\n".join(code_lines(name))
            for forbidden in (
                r"-Verb\s+RunAs",
                r"RunAs",
                r"Start-Process[^\n]*-Credential",
                r"-Credential\b",
                r"runas\.exe",
                r"Invoke-Command[^\n]*-ComputerName",
                r"New-ScheduledTaskPrincipal[^\n]*-RunLevel\s+Highest",
                r"\[Environment\]::SetEnvironmentVariable\([^)]*\"(User|Machine)\"",
                r"setx\b",
                r"DB_PASSWORD\s*=|DJANGO_SECRET_KEY\s*=|\$env:(DB_PASSWORD|DJANGO_SECRET_KEY|EMAIL_HOST_PASSWORD)\s*=",
            ):
                self.assertIsNone(re.search(forbidden, code, re.I), (name, forbidden))
        setup = "\n".join(code_lines("Setup-CovensLibrary.ps1"))
        self.assertNotIn("Get-BitLockerVolume", setup)  # the owner never reads BitLocker directly
        self.assertNotIn("sdset", setup)  # no service security change in the owner's setup
        self.assertNotIn("-GrantDatabaseServiceControl", setup)

    def test_owner_setup_verifies_the_credential_manager_the_shortcuts_and_a_trial_start(self):
        code = "\n".join(code_lines("Setup-CovensLibrary.ps1"))
        self.assertIn('store_secrets.py") --check', code)
        self.assertIn("There is no fallback to environment variables", code)
        self.assertIn("Desktop shortcuts (on this account's desktop)", code)
        self.assertIn('Start-CovensLibrary.ps1") -Silent -NoBrowser', code)
        self.assertIn("Get-AppHealth", code)
        self.assertIn('Stop-CovensLibrary.ps1") -Silent', code)
        self.assertIn("Confirm-BitLockerRecoveryKeyStored", code)

    def test_only_the_two_administrator_scripts_perform_administrator_operations(self):
        admin_only = {self.ADMIN, self.GRANT}
        for path in WIN.glob("*.ps1"):
            code = "\n".join(code_lines(path.name))
            if path.name in admin_only:
                self.assertIn("Test-IsElevated", code, path.name)
                continue
            self.assertNotIn("sdset", code, path.name)
            self.assertNotIn(
                "Get-BitLockerVolume",
                code.replace("function Test-BitLockerProtection", ""),
                path.name,
            ) if path.name != "bitlocker.ps1" else None

    def test_the_service_grant_names_the_owner_explicitly_and_confirms(self):
        code = "\n".join(code_lines(self.GRANT))
        self.assertIn("[Parameter(Mandatory = $true)][string]$OwnerAccount", code)
        self.assertIn("Test-IsElevated", code)
        self.assertIn("Translate([System.Security.Principal.SecurityIdentifier])", code)
        self.assertIn("Type the owner account name again to confirm", code)
        self.assertNotIn("Get-UserSid", code)  # never "the current user": that is the administrator
        self.assertNotIn("common.ps1", code)

    def test_the_owner_side_checks_the_machine_record_without_administrator_rights(self):
        body = function_body("bitlocker.ps1", "Assert-BitLockerAttestation")
        for needle in (
            "$a.computer -ne $env:COMPUTERNAME",
            "FullyEncrypted",
            "recovery_protectors",
            "TotalDays -gt 30",
        ):
            self.assertIn(needle, body)
        self.assertNotIn("Get-BitLockerVolume", body)
        confirm = function_body("common.ps1", "Confirm-BitLockerRecoveryKeyStored")
        self.assertIn('-ne "YES"', confirm)
        self.assertIn("$ConfigDir", confirm)  # recorded in the OWNER's profile


class ProductionLockTests(SimpleTestCase):
    LOCK = ROOT / "requirements" / "production.lock"

    def pins(self, path):
        out = {}
        for line in path.read_text().splitlines():
            m = re.match(r"^([A-Za-z0-9_.\[\]-]+)==([^\s;\\]+)", line)
            if m:
                out[re.sub(r"\[.*\]", "", m.group(1)).lower().replace("_", "-")] = m.group(2)
        return out

    def test_every_direct_pin_is_in_the_lock_at_the_same_version(self):
        direct = self.pins(ROOT / "requirements" / "base.txt")
        locked = self.pins(self.LOCK)
        self.assertGreater(len(locked), len(direct))  # transitive packages are pinned as well
        for name, version in direct.items():
            self.assertEqual(locked.get(name), version, name)

    def test_every_locked_package_carries_hashes(self):
        blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", self.LOCK.read_text())
        pinned = [b for b in blocks if re.match(r"[A-Za-z0-9_.-]+==", b)]
        self.assertGreater(len(pinned), 30)
        for block in pinned:
            self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}", block.splitlines()[0])

    def test_windows_only_dependencies_are_present_with_markers(self):
        body = self.LOCK.read_text()
        for needed in ("pywin32-ctypes", "tzdata"):
            self.assertRegex(body, rf"(?m)^{needed}==\S+ ; sys_platform == 'win32'", needed)

    def test_no_development_tool_is_in_the_production_lock(self):
        names = set(self.pins(self.LOCK))
        for dev in ("ruff", "coverage", "python-dotenv", "bandit", "pip-audit"):
            self.assertNotIn(dev, names)

    def test_the_production_settings_do_not_import_development_helpers(self):
        for name in ("production.py", "base.py"):
            self.assertNotIn("dotenv", (ROOT / "config" / "settings" / name).read_text())
