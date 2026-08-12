[CmdletBinding()]
param(
    [string]$Distribution,
    [string]$Repository,
    [string]$Target,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$normalizedScriptRoot = $PSScriptRoot -replace '\\', '/'
if ($normalizedScriptRoot -match '^//(?:wsl\.localhost|wsl\$)/([^/]+)(/.*)/scripts$') {
    if (-not $Distribution) {
        $Distribution = $Matches[1]
    }
    if (-not $Repository) {
        $Repository = $Matches[2]
    }
}
if (-not $Distribution) {
    $Distribution = $env:OCR_WSL_DISTRIBUTION
}
if (-not $Repository) {
    $Repository = $env:OCR_REPOSITORY
}
if (-not $Distribution -or -not $Repository) {
    throw "Could not derive WSL distribution/repository. Pass -Distribution and -Repository."
}
if (-not $Target) {
    $Target = "$Repository/.production/src/ocr_pdf_rebuilder"
}

$arguments = @(
    "-d", $Distribution,
    "--", "bash", "$Repository/scripts/deploy-to-wsl.sh",
    "--target", $Target
)
if ($Apply) {
    $arguments += "--apply"
}

& wsl.exe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "WSL deployment failed with exit code $LASTEXITCODE"
}
