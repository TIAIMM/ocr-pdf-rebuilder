[CmdletBinding()]
param(
    [string]$Distribution = "Ubuntu-24.04-OCR",
    [string]$Repository = "/home/ocr/document-ocr-pipeline",
    [string]$Target = "/home/ocr/ocr_jobs",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
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
