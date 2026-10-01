param(
    [string]$Python = "python",
    [switch]$WithDocling,
    [switch]$Dev,
    [string]$Constraints
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv"
$projectPython = Join-Path $venvRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $projectPython)) {
    if (Test-Path -LiteralPath $venvRoot) {
        throw "An incomplete .venv exists. Inspect it before creating a new environment."
    }
    & $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required." }
    & $Python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create project environment." }
}

$extras = @("gui")
if ($WithDocling) { $extras += "docling" }
if ($Dev) { $extras += "dev" }
$package = "${projectRoot}[$($extras -join ',')]"
$installArgs = @("-m", "pip", "install", "-e", $package)
if (-not $Constraints) {
    $pythonVersion = & $projectPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect project Python." }
    $validated = Join-Path $projectRoot "requirements\windows-py313-constraints.txt"
    if ($pythonVersion -eq "3.13" -and (Test-Path -LiteralPath $validated)) {
        $Constraints = $validated
    }
}
if ($Constraints) {
    Write-Output "Using dependency constraints: $Constraints"
    $installArgs += @("-c", (Resolve-Path -LiteralPath $Constraints).Path)
}
& $projectPython @installArgs
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed; environment is not ready." }
& $projectPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed." }
foreach ($name in @("config.yaml", ".env")) {
    $destination = Join-Path $projectRoot $name
    $template = if ($name -eq "config.yaml") { "config.example.yaml" } else { ".env.example" }
    if (-not (Test-Path -LiteralPath $destination)) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $template) -Destination $destination
    }
}
Write-Output "Project environment ready: $projectPython"
Write-Output "Next: double-click Start-PaperLoom.cmd to open the web interface."
