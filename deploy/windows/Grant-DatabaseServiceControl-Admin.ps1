# OPTIONAL, ADMINISTRATOR-ONLY. Lets ONE named Windows account (the owner) START and STOP only the
# PostgreSQL service, so the morning launcher needs no administrator prompt. Run in an elevated
# PowerShell, NAMING the owner (never "the current user", because the administrator is a different
# account):
#     powershell -ExecutionPolicy Bypass -File deploy\windows\Grant-DatabaseServiceControl-Admin.ps1 -OwnerAccount "COMPUTER\owner"
# It only ADDS one access entry to that one service; nothing in any profile is touched.
# Rehearse it on a test machine first. NOT VERIFIED ON WINDOWS YET.
param([Parameter(Mandatory = $true)][string]$OwnerAccount)
. (Join-Path $PSScriptRoot "bitlocker.ps1")
if (-not (Test-IsElevated)) { Write-Host "Run this in an ADMINISTRATOR PowerShell. Nothing was changed." -ForegroundColor Red; exit 1 }
$svc = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $svc) { Write-Host "No PostgreSQL service found. Nothing was changed." -ForegroundColor Red; exit 1 }
try { $sid = (New-Object System.Security.Principal.NTAccount($OwnerAccount)).Translate([System.Security.Principal.SecurityIdentifier]).Value }
catch { Write-Host "The account '$OwnerAccount' was not found. Nothing was changed." -ForegroundColor Red; exit 1 }
Write-Host "This will let '$OwnerAccount' (SID $sid) start and stop the service '$($svc.Name)' only."
$answer = Read-Host "Type the owner account name again to confirm"
if ($answer -ne $OwnerAccount) { Write-Host "Not confirmed. Nothing was changed." -ForegroundColor Yellow; exit 1 }
$sddl = (& sc.exe sdshow $svc.Name | Out-String).Trim()
if ($sddl -match [regex]::Escape($sid)) { Write-Host "Already granted."; exit 0 }
$ace = "(A;;LCRPWPLORC;;;$sid)"
if ($sddl -match "S:") { $new = $sddl -replace "S:", "${ace}S:" } else { $new = "$sddl$ace" }
& sc.exe sdset $svc.Name $new | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "sc.exe refused the change (exit $LASTEXITCODE). Nothing was changed." -ForegroundColor Red; exit 1 }
Write-Host "Done: '$OwnerAccount' may start and stop '$($svc.Name)'." -ForegroundColor Green
exit 0
