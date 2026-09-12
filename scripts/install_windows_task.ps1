param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [string]$At = "08:00",
    [string]$TaskName = "arXiv Research Assistant"
)

$resolvedProject = (Resolve-Path -LiteralPath $ProjectDir).Path
$python = Join-Path $resolvedProject ".venv\Scripts\python.exe"
$config = Join-Path $resolvedProject "config.yaml"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath $config)) {
    throw "Configuration not found: $config"
}

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m arxiv_ra --config `"$config`" run" `
    -WorkingDirectory $resolvedProject
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Description "Generate the personalized daily arXiv research digest." `
    -Force | Out-Null

Write-Output "Scheduled task '$TaskName' created for $At."

