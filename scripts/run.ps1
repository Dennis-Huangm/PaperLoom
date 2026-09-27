[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$ConfigPath,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs = @("gui")
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python not found: $python. Run scripts\setup_environment.ps1 first."
}
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $projectRoot "config.yaml"
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path

# Match the scheduled task's interpreter and working directory, regardless of
# the shell's active Conda environment or current directory.
Push-Location -LiteralPath $projectRoot
try {
    & $python -m arxiv_ra --config $ConfigPath @CommandArgs
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
