# Checks that this Windows 11 computer is set up as docs\DEPLOYMENT.md describes and prints a
# PASS / FAIL line for each item. It changes nothing. Run it on the Dell after installation and
# keep the output with the rehearsal record (docs\OPERATIONS.md).
# The application must be RUNNING for the last items (start it with the desktop shortcut first).
. (Join-Path $PSScriptRoot "common.ps1")
$results = @()
function Check($name, [scriptblock]$test) {
    try { $ok = & $test } catch { $ok = $false }
    $script:results += [pscustomobject]@{ Check = $name; Result = $(if ($ok) { "PASS" } else { "FAIL" }) }
}
function Info($name, [scriptblock]$test) {
    try { $text = & $test } catch { $text = "could not be determined: $($_.Exception.Message)" }
    $script:results += [pscustomobject]@{ Check = $name; Result = "INFO: $text" }
}
Set-Location $AppRoot
Check "Windows 11 (build 22000 or newer)" { [Environment]::OSVersion.Version.Build -ge 22000 }
Check "Python 3.12 in the virtual environment" { (& $Python --version) -match "3\.12" }
Check "Installed packages match requirements\production.lock exactly" { $want = Get-Content (Join-Path $AppRoot "requirements\production.lock") | Where-Object { $_ -match "^[A-Za-z0-9_.-]+==[^ ]+" } | ForEach-Object { ($_ -replace "\s*;.*$", "" -replace "\s*\\$", "").Trim().ToLower() }; $have = (& $Python -m pip freeze) | ForEach-Object { $_.Trim().ToLower() }; -not ($want | Where-Object { ($have -notcontains $_) -and ($_ -notmatch "^(pywin32-ctypes|tzdata|jeepney|secretstorage)==") }) }
Check "pg_dump and pg_restore found (PATH or PG_DUMP_PATH in app.env)" { Import-AppConfig; ((Get-Command pg_dump -ErrorAction SilentlyContinue) -and (Get-Command pg_restore -ErrorAction SilentlyContinue)) -or ($env:PG_DUMP_PATH -and (Test-Path $env:PG_DUMP_PATH)) }
Check "Configuration is complete, secrets reachable, no owner credentials, no debug" { Assert-Environment; $true }
Check "Data, config and log folders are private to this account (no inherited access)" { foreach ($d in @($DataDir, $ConfigDir, $LogDir, $RunDir)) { $acl = Get-Acl $d; if (-not $acl.AreAccessRulesProtected) { return $false } }; $true }
Check "PostgreSQL listens on 127.0.0.1 only" { -not (Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -notin "127.0.0.1", "::1" }) }
Check "Database security passes as the runtime role" { Import-AppConfig; $r = Invoke-AppPython @("manage.py", "check_db_security"); $r.Code -eq 0 }
Check "Django check --deploy passes (HTTPS warnings are expected on 127.0.0.1)" { Import-AppConfig; $r = Invoke-AppPython @("manage.py", "check", "--deploy", "--fail-level", "ERROR"); $r.Code -eq 0 }
Check "Launcher preflight passes (login, migrations, database security, backup key)" { Import-AppConfig; $r = Invoke-AppPython @("manage.py", "launcher_preflight"); $r.Code -eq 0 }
Check "No notification record of an anonymised member holds personal data" { Import-AppConfig; $r = Invoke-AppPython @("manage.py", "preflight_notifications"); $r.Code -eq 0 }
Check "Backup key is in the Windows Credential Manager" { Import-AppConfig; $r = Invoke-AppPython @("manage.py", "backup_status"); $r.Output -match "Backup key: present" }
Check "Scheduled jobs task is registered" { [bool](Get-ScheduledTask -TaskName "Coven's Library - jobs" -ErrorAction SilentlyContinue) }
Check "Desktop shortcuts exist" { $d = [Environment]::GetFolderPath("Desktop"); (Test-Path (Join-Path $d "Coven's Library.lnk")) -and (Test-Path (Join-Path $d "Stop Library.lnk")) }
Check "The application answers its health check on 127.0.0.1:$Port" { (Get-AppHealth) -eq "ok" }
Check "The application is NOT listening on any other address" { -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -ne "127.0.0.1" }) }
Check "Security headers are sent" { $r = Invoke-WebRequest -UseBasicParsing "http://${HostName}:$Port/accounts/login/"; $r.Headers["Content-Security-Policy"] -and $r.Headers["X-Frame-Options"] -eq "DENY" }
Check "No secret appears in the launcher or application logs" { $hits = Get-ChildItem $LogDir -File -ErrorAction SilentlyContinue | Select-String -Pattern "(?i)(password|secret|token|bearer)\s*[:=]\s*[^\s\[]" ; -not $hits }
Check "An administrator verified BitLocker (full encryption, protection on, recovery protector) on this computer" { Assert-BitLockerAttestation | Out-Null; $true }
Check "The owner confirmed the BitLocker recovery key is stored off this computer" { Test-Path (Join-Path $ConfigDir "bitlocker.attestation") }
Info "BitLocker live status (needs administrator; otherwise see the checks above)" { $r = Test-BitLockerProtection; "$($r.State): $($r.Detail)" }
Info "Running as" { if (Test-IsElevated) { "ADMINISTRATOR - run this check as the owner instead" } else { "$env:USERDOMAIN\$env:USERNAME (not elevated)" } }
$results | Format-Table -AutoSize -Wrap
if ($results.Result -contains "FAIL") { exit 1 }
