# Starts the application without opening a browser or showing windows (for the optional logon task).
# The owner uses the desktop shortcut instead (Start-CovensLibrary.ps1); this is the same launcher.
. (Join-Path $PSScriptRoot "common.ps1")
& (Join-Path $PSScriptRoot "Start-CovensLibrary.ps1") -Silent -NoBrowser
exit $LASTEXITCODE
