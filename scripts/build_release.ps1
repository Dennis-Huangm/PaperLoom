param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw "Project environment missing. Run scripts\setup_environment.ps1 -Dev first."
}
$actualVersion = & $projectPython -c "import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['project']['version'])" (Join-Path $projectRoot "pyproject.toml")
if ($LASTEXITCODE -ne 0) { throw "Unable to read project version." }
if (-not $Version) { $Version = $actualVersion }
if ($actualVersion -ne $Version) { throw "Requested version does not match pyproject.toml." }
$moduleVersion = & $projectPython -c "import arxiv_ra; print(arxiv_ra.__version__)"
if ($LASTEXITCODE -ne 0 -or $moduleVersion -ne $Version) { throw "Module version mismatch." }
$notesPath = Join-Path $projectRoot "docs\RELEASE_NOTES.md"
if (-not (Test-Path -LiteralPath $notesPath) -or (Get-Content -LiteralPath $notesPath -Raw -Encoding UTF8) -notmatch "^# PaperLoom v$([regex]::Escape($Version))\b") {
    throw "Current release notes do not match version $Version."
}

function Assert-InProject([string]$Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped project root: $fullPath"
    }
}

$workRoot = Join-Path $projectRoot "work\release-build-$Version"
$packageName = "paperloom-$Version"
$packageRoot = Join-Path $workRoot $packageName
$wheelRoot = Join-Path $workRoot "wheel"
$releaseRoot = if ($OutputDirectory) { [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory)) } else { Join-Path $projectRoot "release\v$Version" }
Assert-InProject $workRoot
Assert-InProject $releaseRoot
if (Test-Path -LiteralPath $workRoot) { Remove-Item -LiteralPath $workRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $packageRoot, $wheelRoot, $releaseRoot | Out-Null

# Copy only tracked runtime files and the explicit public-document/launcher list.
# Untracked scripts, local review records and private data can never enter the ZIP.
$publicFiles = @(
    ".env.example", ".gitignore", ".github/workflows/daily.yml",
    "README.md", "CONTRIBUTING.md", "CHANGELOG.md", "LICENSE", "pyproject.toml",
    "config.example.yaml", "Setup-PaperLoom.cmd", "Start-PaperLoom.cmd",
    "docs/USAGE.md", "docs/CLI.md", "docs/RELEASE_NOTES.md",
    "scripts/setup_environment.ps1", "scripts/run.ps1", "scripts/start_gui.ps1",
    "scripts/start_gui_pdf_direct.cmd", "scripts/install_windows_task.ps1"
)
$tracked = & git -C $projectRoot ls-files
if ($LASTEXITCODE -ne 0) { throw "Release builds require a Git checkout." }
$selected = @($tracked | Where-Object {
    $_ -in $publicFiles -or $_ -match '^(src|assets|requirements)/'
})
foreach ($required in $publicFiles) {
    if ($required -notin $selected) { throw "Required file is not tracked: $required" }
}
foreach ($relative in $selected) {
    $source = Join-Path $projectRoot $relative
    $destination = Join-Path $packageRoot $relative
    Assert-InProject $destination
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
}

# Build the wheel from the same curated tree as the ZIP, never the working tree.
& $projectPython -m pip wheel $packageRoot --no-deps --no-cache-dir --no-build-isolation --wheel-dir $wheelRoot
if ($LASTEXITCODE -ne 0) { throw "Wheel build failed." }
$wheel = @(Get-ChildItem -LiteralPath $wheelRoot -Filter "*.whl")
if ($wheel.Count -ne 1) { throw "Wheel build must produce exactly one artifact." }

$sourceArchive = Join-Path $releaseRoot "paperloom-$Version-source.zip"
# zipfile includes dotfiles on all Windows versions. Only the selected files are archived;
# wheel-generated build and egg-info directories remain outside this file list.
$manifest = Join-Path $workRoot "source-files.txt"
[System.IO.File]::WriteAllLines($manifest, $selected, [System.Text.UTF8Encoding]::new($false))
$zipScript = @'
from pathlib import Path
import sys, zipfile
root, manifest, archive = map(Path, sys.argv[1:])
files = manifest.read_text(encoding="utf-8").splitlines()
for name in files:
    parts = Path(name).parts
    forbidden = {".env", "config.yaml", "profiles", "run", "work", "build", "release", "backups", "tests", ".git", "__pycache__"}
    if any(p in forbidden or p.endswith(".egg-info") for p in parts) or name.endswith((".pyc", ".pyo")):
        raise SystemExit("Forbidden release file: " + name)
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
    for name in sorted(files):
        out.write(root / name, f"{root.name}/{name}")
with zipfile.ZipFile(archive) as out:
    if out.testzip() is not None:
        raise SystemExit("ZIP integrity check failed")
    if len(out.namelist()) != len(files):
        raise SystemExit("ZIP file count mismatch")
print(f"Source ZIP validated: {len(files)} public files")
'@
$zipScriptPath = Join-Path $workRoot "archive_source.py"
[System.IO.File]::WriteAllText($zipScriptPath, $zipScript, [System.Text.UTF8Encoding]::new($false))
& $projectPython $zipScriptPath $packageRoot $manifest $sourceArchive
if ($LASTEXITCODE -ne 0) { throw "Source archive validation failed." }

$releaseWheel = Join-Path $releaseRoot $wheel[0].Name
Copy-Item -LiteralPath $wheel[0].FullName -Destination $releaseWheel -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "CHANGELOG.md") -Destination $releaseRoot -Force
Copy-Item -LiteralPath $notesPath -Destination (Join-Path $releaseRoot "RELEASE_NOTES_v$Version.md") -Force
$hashLines = @(
    "{0}  {1}" -f (Get-FileHash -LiteralPath $sourceArchive -Algorithm SHA256).Hash, (Split-Path $sourceArchive -Leaf)
    "{0}  {1}" -f (Get-FileHash -LiteralPath $releaseWheel -Algorithm SHA256).Hash, (Split-Path $releaseWheel -Leaf)
)
$hashLines | Set-Content -LiteralPath (Join-Path $releaseRoot "SHA256SUMS.txt") -Encoding ascii
Write-Host "Release ready: $releaseRoot"
$hashLines | ForEach-Object { Write-Host $_ }
