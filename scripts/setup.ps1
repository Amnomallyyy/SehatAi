# scripts/setup.ps1 – Quick setup script for Windows
Write-Host "Setting up Medical Document Pipeline..." -ForegroundColor Green

# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Upgrade pip
python -m pip install --upgrade pip

# Install requirements
pip install -r requirements.txt

# Copy .env if it doesn't exist
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env – please edit it with your API keys." -ForegroundColor Yellow
    Write-Host "Note: OCR.space free tier has a 1MB file limit." -ForegroundColor Yellow
}

Write-Host "Setup complete. Run 'python setup_checks.py' to verify." -ForegroundColor Green