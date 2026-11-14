# Creates (or repairs) the two desktop shortcuts for the CURRENT Windows account:
#   "Coven's Library"  -> starts the application and opens the browser
#   "Stop Library"     -> stops the application
# The shortcuts start the launcher through RunHidden.vbs so no console window flashes on screen.
# NOT VERIFIED ON WINDOWS YET.
param([switch]$Remove)
. (Join-Path $PSScriptRoot "common.ps1")
if (Test-IsElevated) { Write-Host "Run this in a NORMAL PowerShell as the owner (not as administrator): the shortcuts belong on the owner's own desktop." -ForegroundColor Red; exit 1 }
$desktop = [Environment]::GetFolderPath("Desktop")
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
$vbs = Join-Path $PSScriptRoot "RunHidden.vbs"
$items = @(
    @{ Name = "Coven's Library.lnk"; Script = "Start-CovensLibrary.ps1"; Text = "Start Coven's Library and open it in the browser" },
    @{ Name = "Stop Library.lnk";    Script = "Stop-CovensLibrary.ps1";  Text = "Stop Coven's Library" }
)
foreach ($item in $items) {
    $path = Join-Path $desktop $item.Name
    if ($Remove) { Remove-Item -Force $path -ErrorAction SilentlyContinue; continue }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $wscript
    $link.Arguments = "//B //Nologo `"$vbs`" $($item.Script)"
    $link.WorkingDirectory = $AppRoot
    $link.Description = $item.Text
    $link.WindowStyle = 7
    $link.Save()
}
if ($Remove) { Write-Host "Shortcuts removed." } else { Write-Host "Shortcuts created on the desktop: Coven's Library, Stop Library." }
