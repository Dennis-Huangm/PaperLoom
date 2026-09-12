param(
    [string]$Version = "1.2.0"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workRoot = Join-Path $projectRoot "work\release-build-$Version"
$packageRoot = Join-Path $workRoot "arxiv-research-assistant-$Version"
$wheelRoot = Join-Path $workRoot "wheel"
$releaseRoot = Join-Path $projectRoot "release\v$Version"

function Assert-InProject([string]$Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped project root: $fullPath"
    }
}

Assert-InProject $workRoot
Assert-InProject $releaseRoot
if (Test-Path -LiteralPath $workRoot) {
    Remove-Item -LiteralPath $workRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageRoot, $wheelRoot, $releaseRoot | Out-Null

$directories = @(".github", "assets", "docs", "scripts", "src", "tests")
foreach ($name in $directories) {
    $source = Join-Path $projectRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $packageRoot -Recurse
    }
}

$files = @(
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "config.agentict2i.example.yaml",
    "config.example.yaml",
    "LICENSE",
    "pyproject.toml",
    "README.md"
)
foreach ($name in $files) {
    $source = Join-Path $projectRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $packageRoot
    }
}
$currentReleaseNotes = Join-Path $projectRoot "RELEASE_NOTES_v$Version.md"
if (-not (Test-Path -LiteralPath $currentReleaseNotes)) {
    throw "Release notes not found: $currentReleaseNotes"
}
Copy-Item -LiteralPath $currentReleaseNotes -Destination $packageRoot

$qaArtifacts = Join-Path $packageRoot "docs\qa"
if (Test-Path -LiteralPath $qaArtifacts) {
    Remove-Item -LiteralPath $qaArtifacts -Recurse -Force
}

Get-ChildItem -LiteralPath $packageRoot -Directory -Recurse -Force |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $packageRoot -File -Recurse -Force -Include "*.pyc", "*.pyo" |
    Remove-Item -Force

$sourceArchive = Join-Path $workRoot "arxiv-research-assistant-$Version-source.zip"
Compress-Archive -LiteralPath $packageRoot -DestinationPath $sourceArchive -CompressionLevel Optimal

Push-Location $projectRoot
try {
    python -m pip wheel . --no-deps --no-cache-dir --no-build-isolation --wheel-dir $wheelRoot
    if ($LASTEXITCODE -ne 0) { throw "Wheel build failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

$wheel = Get-ChildItem -LiteralPath $wheelRoot -Filter "*.whl" | Select-Object -First 1
if (-not $wheel) { throw "Wheel build produced no artifact" }

$entries = tar -tf $sourceArchive
$forbidden = $entries | Where-Object {
    $_ -match '(^|/)(\.env|config\.yaml)(/|$)' -or
    $_ -match '(^|/)(profiles|run|work|build|release)(/|$)' -or
    $_ -match '(^|/)(__pycache__|\.pytest_cache)(/|$)' -or
    $_ -match '\.(pyc|pyo)$'
}
if ($forbidden) {
    throw "Source archive contains forbidden data or generated files: $($forbidden -join ', ')"
}

$releaseSource = Join-Path $releaseRoot (Split-Path $sourceArchive -Leaf)
$releaseWheel = Join-Path $releaseRoot $wheel.Name
Copy-Item -LiteralPath $sourceArchive -Destination $releaseSource -Force
Copy-Item -LiteralPath $wheel.FullName -Destination $releaseWheel -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "CHANGELOG.md") -Destination $releaseRoot -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "RELEASE_NOTES_v$Version.md") -Destination $releaseRoot -Force

$hashLines = @(
    "{0}  {1}" -f (Get-FileHash $releaseSource -Algorithm SHA256).Hash, (Split-Path $releaseSource -Leaf)
    "{0}  {1}" -f (Get-FileHash $releaseWheel -Algorithm SHA256).Hash, (Split-Path $releaseWheel -Leaf)
)
$hashLines | Set-Content -LiteralPath (Join-Path $releaseRoot "SHA256SUMS.txt") -Encoding ascii

Write-Host "Release ready: $releaseRoot"
$hashLines | ForEach-Object { Write-Host $_ }
