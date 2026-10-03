param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [int]$Port = 8000,
    [string]$ConfigPath
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$launcher = Join-Path $PSScriptRoot 'gui_background.pyw'
if (-not $ConfigPath) { $ConfigPath = Join-Path $projectRoot 'config.yaml' }
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$task = Get-ScheduledTask -TaskName 'PaperLoom GUI (User)' -ErrorAction SilentlyContinue
$taskMatches = $task -and @($task.Actions | Where-Object {
    $_.Arguments.Contains("`"$ConfigPath`"") -and
    $_.Arguments.Contains("`"$launcher`"") -and
    $_.Arguments -match "--port\s+$Port(?:\s|$)"
}).Count -eq 1
if ($taskMatches -and $Action -in @('start', 'restart')) {
    if ($Action -eq 'restart') {
        & $python -m arxiv_ra.desktop_gui stop --config $ConfigPath --port $Port
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        # Let the windowless parent exit before asking IgnoreNew to start it.
        $deadline = (Get-Date).AddSeconds(5)
        while ((Get-ScheduledTask -TaskName $task.TaskName).State -eq 'Running') {
            if ((Get-Date) -gt $deadline) { throw 'Previous task is still exiting; retry shortly.' }
            Start-Sleep -Milliseconds 100
        }
    }
    $current = & $python -m arxiv_ra.desktop_gui status --config $ConfigPath --port $Port | ConvertFrom-Json
    if (-not $current.running) { Start-ScheduledTask -TaskName $task.TaskName }
    $deadline = (Get-Date).AddSeconds(45)
    do {
        $current = & $python -m arxiv_ra.desktop_gui status --config $ConfigPath --port $Port | ConvertFrom-Json
        if ($current.running -and $current.instance.config -eq $ConfigPath -and $current.instance.nonce) {
            Write-Output "PaperLoom runs at http://127.0.0.1:$Port (user logon task)."
            exit 0
        }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    throw 'GUI did not become ready. Inspect run\.desktop-gui for startup errors or port conflicts.'
}
& $python -m arxiv_ra.desktop_gui $Action --config $ConfigPath --port $Port --launcher $launcher
exit $LASTEXITCODE
