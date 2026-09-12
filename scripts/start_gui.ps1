param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
$arguments = @(
    "-m", "arxiv_ra",
    "--config", (Join-Path $projectRoot "config.yaml"),
    "gui", "--port", $Port
)
if ($NoBrowser) {
    $arguments += "--no-browser"
}

& $python @arguments
