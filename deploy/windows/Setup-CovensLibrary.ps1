# STEP 2 OF THE TWO-ACCOUNT INSTALLATION: run by the OWNER, signed in to HER OWN Windows account,
# in a NORMAL PowerShell. NEVER "Run as administrator", and never as a different account:
#     powershell -ExecutionPolicy Bypass -File deploy\windows\Setup-CovensLibrary.ps1
# (Step 1 is deploy\windows\Verify-BitLocker-Admin.ps1, run by an administrator.)
# Everything this script creates belongs to the account that runs it: the settings and logs under
# %LOCALAPPDATA%\CovensLibrary, the secrets in THAT account's Windows Credential Manager, the desktop
# shortcuts on THAT account's desktop. That is why it refuses to run elevated and asks you to type the
# account name: elevation could silently run it as another account and save the secrets in the wrong
# place. It does NOT enable BitLocker, create database roles, run migrations, create or replace the
# backup key, restore anything, or use environment variables for secrets (docs\DEPLOYMENT.md).
# NOT VERIFIED ON WINDOWS YET: run deploy\windows\Test-Syntax.ps1 first, then this, and keep the output.
param([string]$DatabaseName = "coven_library", [switch]$SkipShortcuts)
. (Join-Path $PSScriptRoot "common.ps1")
$step = 0
function Step([string]$Text) { $script:step++; Write-Host ""; Write-Host "[$script:step] $Text" }
function Fail([string]$Text) { Write-Host "STOPPED: $Text" -ForegroundColor Red; exit 1 }

Step "Which Windows account is this?"
if (Test-IsElevated) {
    Fail "This window is running as administrator. Close it, sign in as the OWNER, and open a normal PowerShell (do not choose 'Run as administrator'). Setup must run without elevation so that the secrets are saved in the owner's own Credential Manager."
}
$me = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "You are signed in as: $me"
Write-Host "Profile folder:       $env:USERPROFILE"
Write-Host "Settings and logs will go to $DataDir"
Write-Host "Secrets will be saved in the Credential Manager of THIS account ($me) and nowhere else."
if (-not $env:LOCALAPPDATA.StartsWith($env:USERPROFILE, [StringComparison]::OrdinalIgnoreCase)) { Fail "The application data folder is not inside this account's profile. Sign in normally as the owner and try again." }
$typed = Read-Host "Type the Windows account name that will use Coven's Library every day (you are signed in as $env:USERNAME)"
if ($typed.Trim() -ine $env:USERNAME -and $typed.Trim() -ine $me) { Fail "That is not the account you are signed in as. Sign in as the owner and run this again. Nothing was created." }

Step "Windows version"
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 22000) { Fail "This is not Windows 11 (build $build). The application is supported on Windows 11 only." }
Write-Host "Windows build $build - ok"

Step "BitLocker (verified by an administrator, confirmed by you)"
try { $attestation = Assert-BitLockerAttestation }
catch {
    Write-Host $_.Exception.Message -ForegroundColor Yellow
    Fail "BitLocker has not been verified for production use. An administrator must run deploy\windows\Verify-BitLocker-Admin.ps1 (in an administrator PowerShell), then you run this again. Nothing was created. BitLocker was NOT enabled by this script."
}
Write-Host "An administrator ($($attestation.verified_by_administrator)) verified on $($attestation.checked_utc): drive $($attestation.drive) fully encrypted, protection on, recovery protector present."
Initialize-DataDir
if (-not (Confirm-BitLockerRecoveryKeyStored $attestation)) { Fail "The BitLocker recovery-key storage was not confirmed. Production setup stops here." }

Step "Python 3.12"
$py312 = $null
foreach ($candidate in @(@("py", "-3.12"), @("python"))) {
    try {
        $exe = $candidate[0]; $rest = @(); if ($candidate.Count -gt 1) { $rest = $candidate[1..($candidate.Count - 1)] }
        $v = & $exe @rest --version 2>&1 | Out-String
        if ($v -match "Python 3\.12\.") { $py312 = @{ Exe = $exe; Args = $rest }; break }
    } catch { }
}
if (-not $py312) { Fail "Python 3.12 was not found. Install it from python.org (tick 'Add python.exe to PATH') and run this again." }
Write-Host "Python 3.12 found - ok"

Step "PostgreSQL 16"
$pgBin = $null
foreach ($root in @("C:\Program Files\PostgreSQL\16\bin", "C:\Program Files (x86)\PostgreSQL\16\bin")) { if (Test-Path (Join-Path $root "pg_dump.exe")) { $pgBin = $root; break } }
if (-not $pgBin) { Fail "PostgreSQL 16 (pg_dump.exe, pg_restore.exe) was not found under C:\Program Files\PostgreSQL\16\bin." }
$svc = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $svc) { Fail "No PostgreSQL service is installed. Install PostgreSQL 16 as a Windows service." }
Write-Host "PostgreSQL service '$($svc.Name)' is $($svc.Status); tools in $pgBin - ok"
Write-Host "(Letting the owner start and stop that service without an administrator prompt is a separate, optional administrator step: Grant-DatabaseServiceControl-Admin.ps1.)"

Step "Configuration in this account's profile"
if (-not (Test-Path $ConfigFile)) {
    $lines = @(
        "# Coven's Library - NON-SECRET settings (secrets are in the Windows Credential Manager).",
        "# Only this Windows account can open this folder. Do not add passwords here.",
        "DB_NAME=$DatabaseName", "DB_USER=coven_app", "DB_HOST=127.0.0.1", "DB_PORT=5432",
        "DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost",
        "PG_DUMP_PATH=$(Join-Path $pgBin 'pg_dump.exe')",
        "BACKUP_DESTINATION=drive"
    )
    Set-Content -Path $ConfigFile -Value $lines -Encoding UTF8
    Write-Host "Created $ConfigFile"
} else { Write-Host "Kept the existing $ConfigFile" }

Step "Python environment and pinned packages"
$lock = Join-Path $AppRoot "requirements\production.lock"
if (-not (Test-Path $lock)) { Fail "requirements\production.lock is missing." }
if (-not (Test-Path $Python)) { & $py312.Exe @($py312.Args) -m venv (Join-Path $AppRoot ".venv"); if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." } }
& $Python -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r $lock
if ($LASTEXITCODE -ne 0) { Fail "Installing the pinned packages failed (see the lines above)." }
Write-Host "Packages installed exactly as pinned (hashes checked) - ok"

Step "Access to THIS account's Credential Manager"
& $Python (Join-Path $PSScriptRoot "store_secrets.py") --check
if ($LASTEXITCODE -ne 0) { Fail "This account's Windows Credential Manager could not be written and read back. No secret was saved. Fix that (sign in normally as the owner) and run this again. There is no fallback to environment variables." }

Step "Secrets in the Windows Credential Manager of $me"
Write-Host "You will be asked for the password of the database role coven_app (and, optionally, the e-mail password). Nothing is shown or saved to a file."
& $Python (Join-Path $PSScriptRoot "store_secrets.py")
if ($LASTEXITCODE -ne 0) { Fail "The secrets were not saved." }

Step "Display files"
Import-AppConfig
$r = Invoke-AppPython @("manage.py", "collectstatic", "--noinput")
if ($r.Code -ne 0) { Fail "Collecting the display files failed: $($r.Output)" }
Write-Host "Display files collected - ok"

if (-not $SkipShortcuts) {
    Step "Desktop shortcuts (on this account's desktop)"
    & (Join-Path $PSScriptRoot "New-Shortcuts.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "The shortcuts could not be created." }
}

Step "Can the application start under this account? (read-only checks, then a trial start)"
$one = Invoke-AppPython @("-m", "core.preflight"); Write-Host "Configuration, secrets and database reachable: exit $($one.Code) $($one.Output)"
$two = Invoke-AppPython @("manage.py", "launcher_preflight"); Write-Host "Login, migrations, database security, backup key: exit $($two.Code) $($two.Output)"
if ($one.Code -ne 0 -or $two.Code -ne 0) {
    Write-Host ""
    Write-Host "Installation steps are done, but a check did not pass. This is expected until the database roles, migrations and the backup key exist (docs\DEPLOYMENT.md sections 2, 4 and 5); then run Setup again, and verify_runtime.ps1." -ForegroundColor Yellow
    exit 2
}
& (Join-Path $PSScriptRoot "Start-CovensLibrary.ps1") -Silent -NoBrowser
$health = Get-AppHealth
& (Join-Path $PSScriptRoot "Stop-CovensLibrary.ps1") -Silent
if ($health -ne "ok") { Fail "The application did not answer its health check when started under this account (see $LauncherLog)." }
Write-Host "The application started under $me, answered its health check and was stopped again - ok"
Write-Host ""
Write-Host "Installation complete. Next: deploy\windows\register_tasks.ps1 (backups), then verify_runtime.ps1." -ForegroundColor Green
