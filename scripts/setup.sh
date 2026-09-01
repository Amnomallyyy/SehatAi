#!/bin/bash
# scripts/setup.sh – Quick setup script for Unix/Linux

echo "Setting up Medical Document Pipeline..."

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install requirements
pip install -r requirements.txt

# Copy .env if it doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env – please edit it with your API keys."
    echo "Note: OCR.space free tier has a 1MB file limit."
fi

echo "Setup complete. Run 'python setup_checks.py' to verify."