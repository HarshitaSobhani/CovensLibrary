# Syntax-checks every PowerShell script in this folder with the real PowerShell parser. It only
# READS the files (nothing is run). Run it on Windows 11 after copying the application:
#     powershell -ExecutionPolicy Bypass -File deploy\windows\Test-Syntax.ps1
# Exit code 0 = every script parses; 1 = at least one syntax error (listed). This is the check the
# development machine cannot do: keep its output with the installation record.
$failures = 0
foreach ($file in Get-ChildItem -Path $PSScriptRoot -Filter *.ps1) {
    $tokens = $null; $problems = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$problems)
    if ($problems.Count -gt 0) {
        $failures += $problems.Count
        foreach ($p in $problems) { Write-Host "FAIL $($file.Name):$($p.Extent.StartLineNumber) $($p.Message)" -ForegroundColor Red }
    } else { Write-Host "OK   $($file.Name)" }
}
if ($failures -gt 0) { Write-Host "$failures syntax error(s)." -ForegroundColor Red; exit 1 }
Write-Host "All scripts parse." -ForegroundColor Green
exit 0
