param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [string]$At = "08:00",
    [switch]$MultiProfile,
    # Reuse the existing task instead of scheduling a duplicate after the rebrand.
    [string]$TaskName = "arXiv Research Assistant"
)

$ErrorActionPreference = "Stop"
$resolvedProject = (Resolve-Path -LiteralPath $ProjectDir).Path
$python = Join-Path $resolvedProject ".venv\Scripts\python.exe"
$config = Join-Path $resolvedProject "config.yaml"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath $config)) {
    throw "Configuration not found: $config"
}

# Validate the exact interpreter/configuration the task will use. This is an
# offline check and does not send mail, call a model, or dispatch scheduled work.
& $python -m arxiv_ra --config $config doctor
if ($LASTEXITCODE -ne 0) {
    throw "Project environment check failed; scheduled task was not registered."
}

$entryCommand = if ($MultiProfile) { "schedule --once" } else { "run" }
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m arxiv_ra --config `"$config`" $entryCommand" `
    -WorkingDirectory $resolvedProject
$trigger = if ($MultiProfile) {
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
} else {
    New-ScheduledTaskTrigger -Daily -At $At
}
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "PaperLoom: generate the personalized daily research digest." `
    -Force | Out-Null

if ($MultiProfile) {
    Write-Output "Scheduled task '$TaskName' checks explicit profile schedules every 5 minutes."
} else {
    Write-Output "Scheduled task '$TaskName' created for $At."
}
