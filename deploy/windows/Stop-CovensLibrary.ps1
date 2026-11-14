# Stops Coven's Library: double-click "Stop Library" on the desktop.
# Asks the server to finish what it is doing and exit (a request file the server watches). It only
# ever ends the exact process the server recorded about itself, and only after the polite request
# has not worked and that process has been identified (never by name, never another Python).
# NOT VERIFIED ON WINDOWS YET (see docs\DEPLOYMENT.md section 11).
param([switch]$Silent)
. (Join-Path $PSScriptRoot "common.ps1")
Add-Type -AssemblyName System.Windows.Forms
function Tell([string]$Text, [string]$Icon = "Information") {
    Write-Log "stop: $Text"
    if (-not $Silent) { [void][System.Windows.Forms.MessageBox]::Show($Text, "Coven's Library", "OK", $Icon) }
}

try {
    Initialize-DataDir
    Write-Log "---- stop requested"
    $state = Get-AppHealth
    $instanceFile = Join-Path $RunDir "server.json"
    $instance = $null
    if (Test-Path $instanceFile) { try { $instance = Get-Content $instanceFile -Raw | ConvertFrom-Json } catch { } }
    if ($state -eq "down") {
        if (Test-Path $instanceFile) { Remove-Item -Force $instanceFile -ErrorAction SilentlyContinue }
        Tell "Coven's Library is not running."; exit 0
    }
    if ($state -eq "foreign") { Tell "Another program is using that place. Coven's Library was not touched." "Warning"; exit 1 }

    Set-Content -Path (Join-Path $RunDir "stop.request") -Value (Get-Date).ToString("o")
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        if ((Get-AppHealth) -eq "down") { Tell "Coven's Library has been stopped."; exit 0 }
        Start-Sleep -Milliseconds 500
    }

    # The polite request did not work: end ONLY the process the server recorded, after identifying it.
    if (-not $instance) { Tell "Coven's Library did not stop. Please restart the computer, or contact support." "Warning"; exit 1 }
    # On Windows the venv's python.exe starts the real interpreter as a child, so the recorded pid
    # (the server's own) is that child. Identify it by pid + start time + command line, not by name.
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $($instance.pid)" -ErrorAction SilentlyContinue
    $started = [DateTimeOffset]::FromUnixTimeSeconds([int64]$instance.started).LocalDateTime
    $identified = $false
    if ($p -and ($p.CommandLine -like "*manage.py*serve_local*") -and ($p.Name -ieq "python.exe")) {
        $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
        if ($proc -and ([Math]::Abs(($proc.StartTime - $started).TotalSeconds) -lt 180)) { $identified = $true }
    }
    if ($identified) {
        Write-Log "stop: asking did not work; ending the recorded server process $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force
        Start-Sleep -Seconds 2
        Remove-Item -Force $instanceFile, (Join-Path $RunDir "stop.request") -ErrorAction SilentlyContinue
        Tell "Coven's Library has been stopped."
        exit 0
    }
    Tell "Coven's Library did not stop and could not be identified safely. Please restart the computer, or contact support." "Warning"
    exit 1
} catch {
    Write-Log "stop FAILED: $($_.Exception.Message)"
    Tell "Could not stop the application. Contact support." "Warning"
    exit 1
}
