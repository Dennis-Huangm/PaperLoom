param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$arguments = @("gui", "--port", "$Port")
if ($NoBrowser) {
    $arguments += "--no-browser"
}

& (Join-Path $PSScriptRoot "run.ps1") -CommandArgs $arguments
exit $LASTEXITCODE
