# Run the WebRTC bot on Windows (avoids WSL->Chrome audio routing issues).
Set-Location $PSScriptRoot

# .venv from WSL/Linux has no Windows python.exe — recreate if needed
$winPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $winPython)) {
    Write-Host "Recreating Windows virtual environment (WSL .venv is not usable on Windows)..."
    if (Test-Path ".venv") {
        Remove-Item -Recurse -Force ".venv"
    }
}

uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv run bot-openai.py --transport webrtc --host localhost
