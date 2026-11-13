# Checks that this Windows 11 computer is set up as docs\DEPLOYMENT.md describes and prints a
# PASS / FAIL line for each item. It changes nothing. Run it on the Dell after installation and
# keep the output with the rehearsal record (docs\OPERATIONS.md).
. (Join-Path $PSScriptRoot "common.ps1")
$results = @()
function Check($name, [scriptblock]$test) {
    try { $ok = & $test } catch { $ok = $false }
    $script:results += [pscustomobject]@{ Check = $name; Result = $(if ($ok) { "PASS" } else { "FAIL" }) }
}
Set-Location $AppRoot
Check "Windows 11 (build 22000 or newer)" { [Environment]::OSVersion.Version.Build -ge 22000 }
Check "Python 3.12 in the virtual environment" { (& $Python --version) -match "3\.12" }
Check "pg_dump and pg_restore on PATH (or PG_DUMP_PATH set)" { (Get-Command pg_dump -ErrorAction SilentlyContinue) -and (Get-Command pg_restore -ErrorAction SilentlyContinue) -or $env:PG_DUMP_PATH }
Check "Runtime environment is complete and has no owner credentials" { Assert-Environment; $true }
Check "PostgreSQL listens on 127.0.0.1 only" { -not (Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -notin "127.0.0.1", "::1" }) }
Check "Database security passes as the runtime role" { & $Python manage.py check_db_security | Out-Null; $LASTEXITCODE -eq 0 }
Check "Django deployment checks pass" { & $Python manage.py check --deploy --fail-level ERROR | Out-Null; $LASTEXITCODE -eq 0 }
Check "No notification record of an anonymised member holds personal data" { & $Python manage.py preflight_notifications | Out-Null; $LASTEXITCODE -eq 0 }
Check "Backup key is in the Windows Credential Manager" { ((& $Python manage.py backup_status) -join "`n") -match "Backup key: present" }
Check "Scheduled tasks are registered" { (Get-ScheduledTask -TaskName "Coven's Library - application" -ErrorAction SilentlyContinue) -and (Get-ScheduledTask -TaskName "Coven's Library - jobs" -ErrorAction SilentlyContinue) }
Check "The application answers on 127.0.0.1:$Port" { (Invoke-WebRequest -UseBasicParsing "http://${HostName}:$Port/accounts/login/").StatusCode -eq 200 }
Check "The application is NOT listening on any other address" { -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -ne "127.0.0.1" }) }
Check "Security headers are sent" { $r = Invoke-WebRequest -UseBasicParsing "http://${HostName}:$Port/accounts/login/"; $r.Headers["Content-Security-Policy"] -and $r.Headers["X-Frame-Options"] -eq "DENY" }
$results | Format-Table -AutoSize
if ($results.Result -contains "FAIL") { exit 1 }
