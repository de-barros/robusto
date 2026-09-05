param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Set-Location -Path $PSScriptRoot

if ($PSScriptRoot -match "\\(Dropbox|OneDrive|iCloudDrive)\\") {
    Write-Warning "This checkout is inside a cloud-synced folder. If pip reports WinError 32 or locked .pyc files, move the clone or create the virtual environment outside the synced folder."
}

if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete."
Write-Host "Activate with: .\.venv\Scripts\Activate.ps1"
Write-Host "Run tests with: python -m unittest"

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Warning "Claude Code CLI was not found on PATH. Install it and run 'claude' once to authenticate before scripts\review_paper.py."
}
