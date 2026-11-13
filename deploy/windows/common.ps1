# Shared settings for the Coven's Library scripts (Windows 11, PowerShell 5.1 or 7).
# NOT VERIFIED ON WINDOWS YET (Phase 7c): run deploy\windows\verify_runtime.ps1 on the Dell.
# This file contains no secrets. The runtime environment variables (DJANGO_SETTINGS_MODULE,
# DJANGO_SECRET_KEY, DB_NAME, DB_USER, DB_PASSWORD, SMTP settings) are set for the Windows account
# that runs the application, see docs\DEPLOYMENT.md. Owner database credentials are never set here.
$ErrorActionPreference = "Stop"
$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $AppRoot ".venv\Scripts\python.exe"
$Port = 8000
$HostName = "127.0.0.1"   # never anything else: the application is not exposed to the network

function Assert-Environment {
    if (-not (Test-Path $Python)) { throw "Python virtual environment not found at $Python" }
    foreach ($name in "DJANGO_SETTINGS_MODULE", "DJANGO_SECRET_KEY", "DB_NAME", "DB_USER", "DB_PASSWORD") {
        if (-not [Environment]::GetEnvironmentVariable($name)) { throw "Environment variable $name is not set for this account." }
    }
    if ($env:DJANGO_SETTINGS_MODULE -ne "config.settings.production") { throw "DJANGO_SETTINGS_MODULE must be config.settings.production." }
    if ($env:DB_USER -ne "coven_app") { throw "The application must run as the runtime database role coven_app." }
    if ([Environment]::GetEnvironmentVariable("DB_OWNER_USER") -or [Environment]::GetEnvironmentVariable("DB_OWNER_PASSWORD")) {
        throw "Owner database credentials must NOT be present in the runtime environment."
    }
}
