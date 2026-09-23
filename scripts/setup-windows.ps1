$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv-transformers\Scripts\python.exe'
Push-Location $repoRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv is required to install the Windows GPU environment.'
    }
    if (-not (Test-Path -LiteralPath $python)) {
        uv venv .venv-transformers --python 3.12 --managed-python
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv-transformers.' }
    }
    uv pip install --python $python 'torch==2.11.0+cu128' 'torchvision==0.26.0+cu128' --index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { throw 'Could not install CUDA PyTorch.' }
    uv pip install --python $python -e '.[windows-transformers]'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install OCR reconstruction dependencies.' }
    $env:OCR_RUNTIME_ROOT = $repoRoot
    & $python -c 'from ocr_pdf_rebuilder.paddle_pipeline import preflight_windows_native; preflight_windows_native()'
    if ($LASTEXITCODE -ne 0) { throw 'Windows GPU/model/font preflight failed.' }
    Write-Host 'Windows Transformers environment is ready.'
} finally {
    Pop-Location
}
