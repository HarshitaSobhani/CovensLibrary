# The scheduled job: overdue forfeitures, notification cycle (reminders, retries) and the daily
# backup when due (with catch-up). Idempotent and safe to run every few minutes. It does not need the
# web server to be running.
. (Join-Path $PSScriptRoot "common.ps1")
Assert-Environment
Set-Location $AppRoot
& $Python manage.py run_daily_jobs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
