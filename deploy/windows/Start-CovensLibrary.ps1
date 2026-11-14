# One-click start for the owner: double-click "Coven's Library" on the desktop.
# Checks everything, starts the EXISTING production server (waitress on 127.0.0.1 only) and opens the
# browser only after the application has answered its health check. Normal start NEVER runs
# migrations, creates keys, changes passwords or permissions, restores backups or installs packages.
# Technical details go to %LOCALAPPDATA%\CovensLibrary\logs\launcher.log, never to the screen.
# NOT VERIFIED ON WINDOWS YET (see docs\DEPLOYMENT.md section 11).
param([switch]$Silent, [switch]$NoBrowser)
. (Join-Path $PSScriptRoot "common.ps1")

$ui = $null
function Show-Status([string]$Text) {
    Write-Log "status: $Text"
    if ($Silent) { return }
    if (-not $script:ui) {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "Coven's Library"
        $form.StartPosition = "CenterScreen"; $form.TopMost = $true; $form.ShowInTaskbar = $false
        $form.FormBorderStyle = "FixedDialog"; $form.ControlBox = $false
        $form.ClientSize = New-Object System.Drawing.Size(420, 90)
        $label = New-Object System.Windows.Forms.Label
        $label.AutoSize = $false; $label.TextAlign = "MiddleCenter"
        $label.Dock = "Fill"; $label.Font = New-Object System.Drawing.Font("Segoe UI", 12)
        $form.Controls.Add($label); $form.Show()
        $script:ui = @{ Form = $form; Label = $label }
    }
    $script:ui.Label.Text = $Text
    [System.Windows.Forms.Application]::DoEvents()
}
function Pump { if ($script:ui) { [System.Windows.Forms.Application]::DoEvents() } }
function Close-Status { if ($script:ui) { try { $script:ui.Form.Close() } catch { } ; $script:ui = $null } }
function Stop-WithMessage([string]$Text, [string]$Technical) {
    Write-Log "FAILED: $Text | $Technical"
    Close-Status
    if (-not $Silent) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show("$Text`r`n`r`nCould not start the application. Contact support.", "Coven's Library", "OK", "Warning")
    }
    exit 1
}
function Open-Browser { if (-not $Silent -and -not $NoBrowser) { Start-Process "http://${HostName}:$Port/" } }

try {
    Initialize-DataDir
    Write-Log "---- start requested (silent=$Silent)"
    Show-Status "Starting Coven's Library..."

    # 1. Already running? Then just open it. Never start a second copy, never touch another program.
    $state = Get-AppHealth
    if ($state -eq "ok") {
        Show-Status "Coven's Library is already running."
        Open-Browser; Start-Sleep -Seconds 1; Close-Status; exit 0
    }
    if ($state -eq "unhealthy") { Stop-WithMessage "Coven's Library is running but is not working correctly." "healthz reports unavailable" }
    if ($state -eq "foreign") { Stop-WithMessage "Another program is using the place Coven's Library needs." "port $Port answers but is not Coven's Library" }

    # A server that ended unexpectedly leaves its state file behind: note it, the server clears it.
    $instanceFile = Join-Path $RunDir "server.json"
    if (Test-Path $instanceFile) {
        $old = $null; try { $old = Get-Content $instanceFile -Raw | ConvertFrom-Json } catch { }
        if ($old -and -not (Get-Process -Id $old.pid -ErrorAction SilentlyContinue)) { Write-Log "previous server (pid $($old.pid)) ended unexpectedly; starting a new one" }
    }

    # 2. Files and Python packages.
    if (-not (Test-Path $Python)) { Stop-WithMessage "The application is not installed correctly." "virtual environment missing: $Python" }
    try { Import-AppConfig } catch { Stop-WithMessage "The application is not set up correctly on this computer." $_.Exception.Message }
    $deps = Invoke-AppPython @("-c", "import django, waitress, psycopg, whitenoise, keyring, reportlab, openpyxl")
    if ($deps.Code -ne 0) { Stop-WithMessage "The application needs to be repaired." "required Python packages are missing: $($deps.Output)" }

    # 3. PostgreSQL: start the service if it is stopped and this account is allowed to.
    Show-Status "Checking the database..."
    $svc = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch "(?i)launcher|stackbuilder" } | Select-Object -First 1
    if ($svc -and $svc.Status -ne "Running") {
        Write-Log "PostgreSQL service $($svc.Name) is $($svc.Status); starting it"
        try { Start-Service -Name $svc.Name } catch { Write-Log "service start refused: $($_.Exception.Message)" }
    }
    $dbPort = 5432; if ($env:DB_PORT) { $dbPort = [int]$env:DB_PORT }
    $deadline = (Get-Date).AddSeconds(30); $up = $false
    while ((Get-Date) -lt $deadline) {
        try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $dbPort); $up = $c.Connected; $c.Close() } catch { }
        if ($up) { break }
        Pump; Start-Sleep -Milliseconds 500
    }
    if (-not $up) { Stop-WithMessage "The database is not running." "nothing listens on 127.0.0.1:$dbPort" }

    # 4. Read-only preflight: configuration and secrets, then login, migrations, database security, key.
    $one = Invoke-AppPython @("-m", "core.preflight")
    if ($one.Code -ne 0) {
        $msg = "The application is not set up correctly on this computer."
        try { $msg = ($one.Output | ConvertFrom-Json).owner } catch { }
        Stop-WithMessage $msg "preflight stage one: $($one.Output)"
    }
    $two = Invoke-AppPython @("manage.py", "launcher_preflight")
    if ($two.Code -ne 0) {
        $msg = "The application could not be checked."
        try { $msg = ($two.Output | ConvertFrom-Json).owner } catch { }
        Stop-WithMessage $msg "preflight stage two: exit $($two.Code) $($two.Output)"
    }

    # 5. Start the existing production-style server, hidden, with its output in protected log files.
    Show-Status "Starting the application..."
    $out = Join-Path $LogDir "server.out.log"; $err = Join-Path $LogDir "server.err.log"
    $proc = Start-Process -FilePath $Python -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @("manage.py", "serve_local", "--host", $HostName, "--port", "$Port") `
        -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Log "server process started: pid $($proc.Id)"

    # 6. Open the browser only when the application itself says it is healthy.
    $deadline = (Get-Date).AddSeconds(90); $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { break }
        if ((Get-AppHealth) -eq "ok") { $ready = $true; break }
        Pump; Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        $code = $null; if ($proc.HasExited) { $code = $proc.ExitCode }
        if ($code -eq 3) { Show-Status "Coven's Library is already running."; Open-Browser; Start-Sleep -Seconds 1; Close-Status; exit 0 }
        Stop-WithMessage "The application did not start." "server exit code '$code' or no healthy answer within 90 seconds (see server.err.log)"
    }
    Show-Status "Coven's Library is ready."
    Write-Log "ready"
    Open-Browser
    Start-Sleep -Seconds 2
    Close-Status
    exit 0
} catch {
    Stop-WithMessage "The application could not be started." $_.Exception.Message
}
