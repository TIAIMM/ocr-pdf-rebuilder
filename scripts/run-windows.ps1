$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv-transformers\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Windows environment missing: $python. Run scripts\setup-windows.ps1 first."
}
$env:OCR_RUNTIME_ROOT = $repoRoot
$env:PADDLEOCR_PYTHON = $python
$env:PADDLEOCR_VL_BACKEND = 'native'
$env:PADDLEOCR_ENGINE = 'transformers'
$env:PADDLEOCR_MODEL_ROOT = Join-Path $repoRoot 'models'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $python -c 'from ocr_pdf_rebuilder.paddle_pipeline import preflight_windows_native; preflight_windows_native()'
if ($LASTEXITCODE -ne 0) { throw 'Windows GPU/model/font preflight failed.' }
Write-Host "Windows input:  $(Join-Path $repoRoot 'input')"
Write-Host "Windows output: $(Join-Path $repoRoot 'pdf_paddle')"
& $python -m ocr_pdf_rebuilder.gui @args
exit $LASTEXITCODE
