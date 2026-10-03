param(
    [string]$TaskName = 'PaperLoom GUI (User)',
    [int]$Port = 8000,
    [string]$UserId = ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $PSScriptRoot 'gui_background.pyw'
$config = Join-Path $projectRoot 'config.yaml'
foreach ($path in @($pythonw, $launcher, $config)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required file not found: $path" }
}
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$basePrefix = & $python -c 'import sys; print(sys.base_prefix)'
if ($LASTEXITCODE -ne 0) {
    throw 'Cannot locate the base Python installation.'
}
$basePythonw = Join-Path $basePrefix 'pythonw.exe'
if (-not (Test-Path -LiteralPath $basePythonw)) {
    throw 'Cannot locate the base windowless Python launcher.'
}
$action = New-ScheduledTaskAction -Execute $basePythonw `
    -Argument "`"$launcher`" run --config `"$config`" --port $Port" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'PaperLoom windowless GUI for the signed-in user.' -Force | Out-Null
Write-Output "Registered '$TaskName' for $UserId at logon, without elevation."
