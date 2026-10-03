param(
    [string]$UserId = ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this one-time migration from administrator PowerShell after all PaperLoom tasks finish.'
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$service = Get-CimInstance Win32_Service -Filter "Name='paperloom'"
if ($service) {
    $parameters = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Services\paperloom\Parameters'
    $expected = (Resolve-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe')).Path
    if ($parameters.Application -ne $expected) {
        throw 'The paperloom service belongs to another installation; no service was changed.'
    }
}
# Register before stopping the existing service, so a registration failure
# leaves the old startup arrangement intact. The new task still runs Limited.
& (Join-Path $PSScriptRoot 'install_gui_logon_task.ps1') -UserId $UserId
if ($service) {
    $backupDir = Join-Path $projectRoot 'run\.desktop-gui'
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $backup = Join-Path $backupDir 'previous-nssm-startup.json'
    if (-not (Test-Path -LiteralPath $backup)) {
        @{Name=$service.Name;StartMode=$service.StartMode;State=$service.State;UserId=$UserId} |
            ConvertTo-Json | Set-Content -LiteralPath $backup -Encoding UTF8
    }
    Set-Service -Name paperloom -StartupType Disabled
    try {
        Stop-Service -Name paperloom -ErrorAction Stop
        (Get-Service -Name paperloom).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(45))
    } catch {
        # Restore the original startup mode if stopping failed.
        $previous = @{Auto='Automatic';Manual='Manual';Disabled='Disabled'}[$service.StartMode]
        Set-Service -Name paperloom -StartupType $previous
        throw
    }
}
Start-ScheduledTask -TaskName 'PaperLoom GUI (User)'
Write-Output 'Migration complete. NSSM registration is retained but its startup is disabled.'
Write-Output 'The new task runs under your ordinary user account. Open http://127.0.0.1:8000.'
