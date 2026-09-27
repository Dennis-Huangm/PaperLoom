param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "1.4.0",
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw "Project environment missing. Run scripts\setup_environment.ps1 -Dev first."
}
$actualVersion = & $projectPython -c "import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['project']['version'])" (Join-Path $projectRoot "pyproject.toml")
if ($LASTEXITCODE -ne 0 -or $actualVersion -ne $Version) {
    throw "Requested version does not match pyproject.toml: $Version / $actualVersion"
}
$workRoot = Join-Path $projectRoot "work\release-build-$Version"
$packageRoot = Join-Path $workRoot "paperloom-$Version"
$wheelRoot = Join-Path $workRoot "wheel"
$releaseRoot = if ($OutputDirectory) { [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory)) } else { Join-Path $projectRoot "release\v$Version" }

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

$directories = @(".github", "assets", "requirements", "src")
foreach ($name in $directories) {
    $source = Join-Path $projectRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $packageRoot -Recurse
    }
}

# User source distribution: keep runtime entry points and maintained documentation.
# Tests and evaluation tools remain available in the Git repository.
$selectedFiles = @(
    "docs/USAGE.md", "docs/ARCHITECTURE.md", "docs/RELEASE_CHECKLIST.md",
    "scripts/setup_environment.ps1", "scripts/run.ps1", "scripts/start_gui.ps1",
    "scripts/install_windows_task.ps1", "scripts/build_release.ps1"
)
foreach ($relative in $selectedFiles) {
    $source = Join-Path $projectRoot $relative
    $destination = Join-Path $packageRoot $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
}

$files = @(
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "CONTEXT.md",
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

Get-ChildItem -LiteralPath $packageRoot -Directory -Recurse -Force |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") -or $_.Name -like "*.egg-info" } |
    Sort-Object FullName -Descending |
    ForEach-Object {
        Assert-InProject $_.FullName
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
Get-ChildItem -LiteralPath $packageRoot -File -Recurse -Force |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    ForEach-Object {
        Assert-InProject $_.FullName
        Remove-Item -LiteralPath $_.FullName -Force
    }

$sourceArchive = Join-Path $workRoot "paperloom-$Version-source.zip"
Compress-Archive -LiteralPath $packageRoot -DestinationPath $sourceArchive -CompressionLevel Optimal

Push-Location $projectRoot
try {
    & $projectPython -m pip wheel . --no-deps --no-cache-dir --no-build-isolation --wheel-dir $wheelRoot
    if ($LASTEXITCODE -ne 0) { throw "Wheel build failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

$wheel = Get-ChildItem -LiteralPath $wheelRoot -Filter "*.whl" | Select-Object -First 1
if (-not $wheel) { throw "Wheel build produced no artifact" }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($sourceArchive)
try {
    $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
}
finally {
    $archive.Dispose()
}
$packageName = Split-Path $packageRoot -Leaf
$requiredFiles = @(
    ".github/workflows/daily.yml",
    ".env.example",
    ".gitignore",
    "README.md",
    "pyproject.toml",
    "config.example.yaml",
    "src/arxiv_ra/__init__.py",
    "src/arxiv_ra/cli.py",
    "docs/USAGE.md",
    "scripts/start_gui.ps1",
    "RELEASE_NOTES_v$Version.md"
)
foreach ($requiredFile in $requiredFiles) {
    if ($entries -notcontains "$packageName/$requiredFile") {
        throw "Source archive is missing required file: $requiredFile"
    }
}
$forbidden = $entries | Where-Object {
    $_ -match '(^|/)(\.env|config\.yaml)(/|$)' -or
    $_ -match '(^|/)(profiles|run|work|build|release|backups|tests|\.git)(/|$)' -or
    $_ -match '/docs/(qa|quality)/' -or
    $_ -match '/scripts/evaluate_[^/]+\.py$' -or
    $_ -match '(^|/)[^/]+\.egg-info(/|$)' -or
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
