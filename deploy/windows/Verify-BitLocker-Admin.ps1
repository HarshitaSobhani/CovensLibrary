# STEP 1 OF THE TWO-ACCOUNT INSTALLATION: run by an ADMINISTRATOR, in an elevated PowerShell.
#     powershell -ExecutionPolicy Bypass -File deploy\windows\Verify-BitLocker-Admin.ps1
# It ONLY reads the BitLocker status of the system drive, confirms the drive is FULLY encrypted with
# protection ON and that a recovery protector EXISTS, and writes one small machine-wide record to
#     %ProgramData%\CovensLibrary\bitlocker-attestation.json     (readable by everyone, writable only by administrators)
# so the OWNER can continue in her own account. It does nothing else:
#   * it never enables BitLocker, never changes protectors or recovery settings;
#   * it never displays, exports, copies or logs a recovery key;
#   * it writes NOTHING in the administrator's profile, creates no log in it, and touches no
#     Credential Manager, no secret and no application setting (it does not even load common.ps1).
# The record shows who verified and when. The OWNER must still confirm, in her own account, that the
# BitLocker recovery key is stored OFF this computer (Setup-CovensLibrary.ps1 asks her).
# NOT VERIFIED ON WINDOWS YET.
. (Join-Path $PSScriptRoot "bitlocker.ps1")

if (-not (Test-IsElevated)) {
    Write-Host "This step must run in an ADMINISTRATOR PowerShell (right-click PowerShell > Run as administrator)." -ForegroundColor Red
    Write-Host "Nothing was read and nothing was changed."
    exit 1
}
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Host "Running as administrator account: $who"
Write-Host "This account is only used to READ the BitLocker status. The application, its settings and its secrets belong to the OWNER's account and are NOT created here."
Write-Host ""

$r = Test-BitLockerProtection
Write-Host "BitLocker: $($r.State) - $($r.Detail)"
if ($r.State -ne "Protected") {
    Write-Host ""
    Write-Host (Get-BitLockerOwnerAction $r.State) -ForegroundColor Yellow
    Write-Host "No verification record was written. BitLocker was NOT changed by this script." -ForegroundColor Yellow
    exit 1
}

$null = New-Item -ItemType Directory -Force -Path $MachineAttestationDir
# SYSTEM and Administrators: full control; Users: read and execute only (they can read, not forge).
& icacls.exe $MachineAttestationDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
$record = [ordered]@{
    schema                    = 1
    checked_utc               = (Get-Date).ToUniversalTime().ToString("o")
    computer                  = $env:COMPUTERNAME
    drive                     = $env:SystemDrive
    protection_status         = $r.ProtectionStatus
    volume_status             = $r.VolumeStatus
    recovery_protectors       = $r.RecoveryProtectors
    verified_by_administrator = $who
}
$record | ConvertTo-Json | Set-Content -Path $MachineAttestationFile -Encoding UTF8

Write-Host ""
Write-Host "BitLocker is protecting this drive. Verification recorded in $MachineAttestationFile" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT (the owner, in HER OWN Windows account, NOT as administrator):"
Write-Host "  1. Sign out of this administrator account and sign in as the owner."
Write-Host "  2. Open a normal PowerShell (not 'Run as administrator') and run deploy\windows\Setup-CovensLibrary.ps1."
Write-Host "  3. Setup will ask the owner to confirm that the BitLocker RECOVERY KEY is stored OFF this computer"
Write-Host "     (printed and kept safe, or in her Microsoft account) - not on this computer, not in a Coven's Library"
Write-Host "     backup and not in the Credential Manager. This script never shows that key."
exit 0
