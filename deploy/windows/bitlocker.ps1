# BitLocker helpers (decision S-05: mandatory before the Dell holds production data).
# PURE FUNCTIONS ONLY: dot-sourcing this file creates no folder, writes no file and touches no
# credential, so it is safe in an ADMINISTRATOR session (nothing can land in the administrator's
# profile). All functions are read-only: they NEVER enable BitLocker, never change protectors and
# never read, print, export or log a recovery key - only the protection status, the volume status
# and the NUMBER of recovery protectors.
# NOT VERIFIED ON WINDOWS YET.

$MachineAttestationDir = Join-Path $env:ProgramData "CovensLibrary"
$MachineAttestationFile = Join-Path $MachineAttestationDir "bitlocker-attestation.json"

function Test-IsElevated {
    # True only in an elevated ("Run as administrator") PowerShell.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-BitLockerProtection {
    # Returns @{ State; Detail; ProtectionStatus; VolumeStatus; RecoveryProtectors } where State is one of
    #   Protected | InProgress | Off | Unavailable | NeedsAdministrator | Unknown      (only Protected passes)
    # Protected = protection ON, the volume FULLY encrypted, and at least one recovery protector exists.
    $drive = $env:SystemDrive
    $none = @{ ProtectionStatus = ""; VolumeStatus = ""; RecoveryProtectors = 0 }
    if (-not (Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue)) {
        return $none + @{ State = "Unavailable"; Detail = "BitLocker management is not available on this Windows edition or installation." }
    }
    try { $v = Get-BitLockerVolume -MountPoint $drive -ErrorAction Stop }
    catch {
        if ($_.Exception.Message -match "(?i)access is denied|administrator|elevat|privilege|0x80070005") {
            return $none + @{ State = "NeedsAdministrator"; Detail = "Reading BitLocker status needs an administrator PowerShell." }
        }
        return $none + @{ State = "Unknown"; Detail = "BitLocker status could not be read ($($_.Exception.GetType().Name))." }
    }
    $protection = "$($v.ProtectionStatus)"; $volume = "$($v.VolumeStatus)"
    $recoveryProtectors = @($v.KeyProtector | Where-Object { "$($_.KeyProtectorType)" -eq "RecoveryPassword" }).Count
    $detail = "Drive $drive : protection $protection, volume $volume, $recoveryProtectors recovery protector(s)."
    $base = @{ ProtectionStatus = $protection; VolumeStatus = $volume; RecoveryProtectors = $recoveryProtectors; Detail = $detail }
    if ($volume -match "FullyDecrypted|DecryptionInProgress" -or $protection -match "^(Off|0)$") { return $base + @{ State = "Off" } }
    if ($volume -match "EncryptionInProgress") { return $base + @{ State = "InProgress" } }
    if ($protection -match "^(On|1)$" -and $volume -eq "FullyEncrypted" -and $recoveryProtectors -ge 1) { return $base + @{ State = "Protected" } }
    return $base + @{ State = "Unknown" }
}

function Get-BitLockerOwnerAction([string]$State) {
    switch ($State) {
        "Off"                { "BitLocker is OFF on the system drive. Turn it on: Settings > Privacy & security > Device encryption (Windows 11 Home) or BitLocker drive encryption (Windows 11 Pro). Wait until encryption is finished, and keep the recovery key OFF this computer." }
        "InProgress"         { "BitLocker is still encrypting the drive. Wait until it has finished, then run this again." }
        "Unavailable"        { "This Windows edition cannot manage BitLocker. Use Windows 11 Pro (or a Home device that supports Device encryption) before the Dell is used for production data." }
        "NeedsAdministrator" { "Run deploy\windows\Verify-BitLocker-Admin.ps1 in an administrator PowerShell (it only reads the status)." }
        default              { "BitLocker status could not be confirmed (it must be ON, FULLY encrypted, with a recovery key). Do not use the Dell for production data until it can be." }
    }
}

function Assert-BitLockerAttestation {
    # OWNER-SIDE (no administrator needed): the administrator's machine-wide record must exist, be
    # recent, match THIS computer and show full protection with a recovery protector.
    # Returns the record; throws a plain-language message otherwise.
    if (-not (Test-Path $MachineAttestationFile)) {
        throw "No administrator has verified BitLocker on this computer yet. An administrator must run deploy\windows\Verify-BitLocker-Admin.ps1 first."
    }
    try { $a = Get-Content $MachineAttestationFile -Raw | ConvertFrom-Json } catch { throw "The BitLocker verification record is unreadable. An administrator must run Verify-BitLocker-Admin.ps1 again." }
    if ($a.schema -ne 1) { throw "The BitLocker verification record has an unknown format. An administrator must run Verify-BitLocker-Admin.ps1 again." }
    if ($a.computer -ne $env:COMPUTERNAME -or $a.drive -ne $env:SystemDrive) { throw "The BitLocker verification record is for another computer or drive. An administrator must run Verify-BitLocker-Admin.ps1 here." }
    if ($a.volume_status -ne "FullyEncrypted" -or "$($a.protection_status)" -notmatch "^(On|1)$" -or [int]$a.recovery_protectors -lt 1) { throw "The BitLocker verification record does not show full protection with a recovery key." }
    $checked = [DateTime]::Parse($a.checked_utc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
    if (((Get-Date).ToUniversalTime() - $checked).TotalDays -gt 30) { throw "The BitLocker verification is older than 30 days. An administrator must run Verify-BitLocker-Admin.ps1 again." }
    return $a
}
