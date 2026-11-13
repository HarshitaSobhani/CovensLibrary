# Registers two Task Scheduler tasks for the CURRENT Windows user (the user whose Credential Manager
# holds the backup key). Run in a normal (non-elevated) PowerShell as that user.
#   Coven's Library - application  : starts the application at logon
#   Coven's Library - jobs         : runs the jobs at logon and every 5 minutes (catches up after sleep)
# "Run only when the user is logged on" is deliberate: the backup key lives in that user's
# Credential Manager, so backups need that account to be signed in (docs\OPERATIONS.md).
# NOT VERIFIED ON WINDOWS YET (Phase 7c).
param([switch]$Remove)
. (Join-Path $PSScriptRoot "common.ps1")
$names = @("Coven's Library - application", "Coven's Library - jobs")
if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Tasks removed."
    return
}
Assert-Environment
$ps = (Get-Command powershell.exe).Source
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$app = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\start_app.ps1`""
$jobs = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_jobs.ps1`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$appSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$jobSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $names[0] -Action $app -Trigger $logon -Settings $appSettings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskName $names[1] -Action $jobs -Trigger @($logon, $repeat) -Settings $jobSettings -Principal $principal -Force | Out-Null
Write-Host "Registered: $($names -join ', '). Sign out and in again (or start them from Task Scheduler) to test."
