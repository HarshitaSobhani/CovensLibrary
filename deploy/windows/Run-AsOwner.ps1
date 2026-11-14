# Runs a manage.py command as the OWNER database role (migrations, grants, restores, owner-side
# security checks). The owner password is typed here, hidden, kept only in this process's memory
# and removed afterwards: it is never written to a file (no .env.owner needed on the Dell), never put
# in the launcher's environment and never logged.
#     deploy\windows\Run-AsOwner.ps1 migrate
#     deploy\windows\Run-AsOwner.ps1 check_db_security
#     deploy\windows\Run-AsOwner.ps1 restore_backup <file.cbk>
# The runtime Django/DB settings still come from app.env and the Credential Manager.
# NOT VERIFIED ON WINDOWS YET.
. (Join-Path $PSScriptRoot "common.ps1")
Initialize-DataDir
Import-AppConfig
$secure = Read-Host "Password of the database OWNER role (not shown)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:DB_OWNER_USER = "coven_owner"
    $env:DB_OWNER_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    Set-Location $AppRoot
    & $Python manage.py @args --settings=config.settings.migrate
    $code = $LASTEXITCODE
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item Env:\DB_OWNER_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\DB_OWNER_USER -ErrorAction SilentlyContinue
}
exit $code
