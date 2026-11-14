# Shared helpers for the Coven's Library scripts (Windows 11, Windows PowerShell 5.1 or PowerShell 7).
# NOT VERIFIED ON WINDOWS YET: run deploy\windows\verify_runtime.ps1 on the Dell and record it.
# This file contains NO secrets. Secrets live in the Windows Credential Manager (store_secrets.py);
# non-secret settings live in a protected file under %LOCALAPPDATA%\CovensLibrary\config\app.env.
$ErrorActionPreference = "Stop"
$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $AppRoot ".venv\Scripts\python.exe"
$Port = 8000
$HostName = "127.0.0.1"   # never anything else: the application is not exposed to the network
$DataDir = Join-Path $env:LOCALAPPDATA "CovensLibrary"
$ConfigDir = Join-Path $DataDir "config"
$ConfigFile = Join-Path $ConfigDir "app.env"
$LogDir = Join-Path $DataDir "logs"
$RunDir = Join-Path $DataDir "run"
$LauncherLog = Join-Path $LogDir "launcher.log"

# Settings the launcher may take from app.env. Anything else (and any secret) is refused.
$AllowedConfigKeys = @(
    "DJANGO_ALLOWED_HOSTS", "DB_NAME", "DB_USER", "DB_HOST", "DB_PORT", "BACKUP_DESTINATION",
    "PG_DUMP_PATH", "BACKUP_TMP_DIR", "EMAIL_BACKEND", "EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_TLS",
    "EMAIL_USE_SSL", "EMAIL_HOST_USER", "DEFAULT_FROM_EMAIL", "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE", "STATIC_ROOT"
)

function Get-UserSid { [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value }

function Protect-Folder([string]$Path) {
    # Only the signed-in owner account may open the folder (no inherited access).
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $sid = Get-UserSid
    & icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
}

function Initialize-DataDir {
    foreach ($d in @($DataDir, $ConfigDir, $LogDir, $RunDir)) { Protect-Folder $d }
}

function Write-Log([string]$Message) {
    # The launcher log never receives secrets: only fixed texts, exit codes and technical codes.
    try {
        if ((Test-Path $LauncherLog) -and ((Get-Item $LauncherLog).Length -gt 1MB)) {
            Move-Item -Force $LauncherLog "$LauncherLog.1"
        }
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $LauncherLog -Value "$stamp  $Message"
    } catch { }
}

function Import-AppConfig {
    # Loads app.env (KEY=VALUE, '#' comments) into THIS process only. Existing environment
    # variables win, so a maintainer can still override a value for a test.
    if (-not (Test-Path $ConfigFile)) { throw "CONFIG: $ConfigFile is missing" }
    foreach ($line in Get-Content $ConfigFile) {
        $text = $line.Trim()
        if ($text -eq "" -or $text.StartsWith("#")) { continue }
        $pair = $text.Split("=", 2)
        if ($pair.Count -ne 2) { throw "CONFIG: unreadable line in app.env" }
        $key = $pair[0].Trim(); $value = $pair[1].Trim()
        if ($AllowedConfigKeys -notcontains $key) { throw "CONFIG: '$key' is not allowed in app.env" }
        if (-not [Environment]::GetEnvironmentVariable($key)) { [Environment]::SetEnvironmentVariable($key, $value, "Process") }
    }
    # Fixed by the launcher, never by the file:
    $env:DJANGO_SETTINGS_MODULE = "config.settings.production"
    $env:DJANGO_DEBUG = "false"
    $env:COVEN_SECRET_STORE = "credential-manager"
    $env:COVEN_RUN_DIR = $RunDir
    $env:COVEN_LOG_DIR = $LogDir
    if (-not $env:DJANGO_ALLOWED_HOSTS) { $env:DJANGO_ALLOWED_HOSTS = "127.0.0.1,localhost" }
}

function Invoke-AppPython {
    # Runs the application's Python in the application folder; returns @{ Code; Output }.
    param([string[]]$Arguments)
    Push-Location $AppRoot
    # Windows PowerShell 5.1 turns a native program's stderr into errors; keep going and use the exit code.
    $previous = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        $output = & $Python @Arguments 2>&1 | Out-String
        return @{ Code = $LASTEXITCODE; Output = $output.Trim() }
    } finally { $ErrorActionPreference = $previous; Pop-Location }
}

function Assert-Environment {
    # Used by the scheduled jobs and the checks: configuration complete, secrets reachable, database up.
    Initialize-DataDir
    if (-not (Test-Path $Python)) { throw "Python virtual environment not found at $Python" }
    Import-AppConfig
    $r = Invoke-AppPython @("-m", "core.preflight")
    if ($r.Code -ne 0) { throw "Preflight stage one failed: $($r.Output)" }
}

function Get-AppHealth {
    # 'ok' | 'unhealthy' | 'foreign' | 'down' for http://127.0.0.1:<port>/healthz/
    $listening = $false
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect($HostName, $Port); $listening = $client.Connected; $client.Close()
    } catch { }
    if (-not $listening) { return "down" }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://${HostName}:$Port/healthz/"
        $body = $r.Content | ConvertFrom-Json
        if ($body.app -eq "coven-library" -and $body.status -eq "ok") { return "ok" }
        return "foreign"
    } catch {
        try {
            $body = $_.ErrorDetails.Message | ConvertFrom-Json
            if ($body.app -eq "coven-library") { return "unhealthy" }
        } catch { }
        return "foreign"
    }
}

# ---- BitLocker, owner side (decision S-05) ------------------------------------------------------
# The administrator's step (Verify-BitLocker-Admin.ps1) READS BitLocker and writes a machine-wide
# record; the helpers live in bitlocker.ps1 (pure functions). Here the OWNER, in her own account and
# WITHOUT administrator rights, confirms the recovery key is stored off the computer.
. (Join-Path $PSScriptRoot "bitlocker.ps1")

function Confirm-BitLockerRecoveryKeyStored($Attestation) {
    Write-Host "The BitLocker RECOVERY KEY must be stored OFF this computer: printed and kept safe, or in your Microsoft account."
    Write-Host "Not on this computer, not inside a Coven's Library backup, not in the Credential Manager. This script never shows that key."
    $answer = Read-Host "Type YES when the recovery key is stored off this computer"
    if ($answer -ne "YES") { return $false }
    $lines = @(
        "confirmed_utc=$((Get-Date).ToUniversalTime().ToString('o'))",
        "owner_account=$env:USERDOMAIN\$env:USERNAME",
        "recovery_key_stored_off_computer=confirmed_by_owner",
        "administrator_verification=$($Attestation.checked_utc) by $($Attestation.verified_by_administrator)"
    )
    Set-Content -Path (Join-Path $ConfigDir "bitlocker.attestation") -Value $lines -Encoding UTF8
    Write-Log "bitlocker: administrator verification found; recovery key storage confirmed by the owner"
    return $true
}
