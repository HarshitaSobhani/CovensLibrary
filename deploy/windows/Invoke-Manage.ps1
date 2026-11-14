# Runs a manage.py command as the RUNTIME role with the same configuration and Credential Manager
# secrets the launcher uses (so a maintainer never has to set environment variables by hand):
#     deploy\windows\Invoke-Manage.ps1 backup_status
#     deploy\windows\Invoke-Manage.ps1 check_db_security
#     deploy\windows\Invoke-Manage.ps1 connect_drive
# NOT VERIFIED ON WINDOWS YET.
. (Join-Path $PSScriptRoot "common.ps1")
Initialize-DataDir
Import-AppConfig
Set-Location $AppRoot
& $Python manage.py @args
exit $LASTEXITCODE
