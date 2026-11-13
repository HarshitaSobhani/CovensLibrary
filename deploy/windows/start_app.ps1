# Starts the application (waitress on 127.0.0.1 only). A launch catch-up runs first.
. (Join-Path $PSScriptRoot "common.ps1")
Assert-Environment
Set-Location $AppRoot
& $Python manage.py serve_local --host $HostName --port $Port
