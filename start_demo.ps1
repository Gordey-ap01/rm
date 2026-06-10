Push-Location $PSScriptRoot

Write-Host "=== Rehab Center Demo Startup (PowerShell) ===" -ForegroundColor Cyan
Write-Host ""

# 1. Create venv if missing
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "[1/5] Creating virtual environment..." -ForegroundColor Yellow
    py -3.11 -m venv .venv
} else {
    Write-Host "[1/5] Virtual environment found." -ForegroundColor Green
}

# 2. Install dependencies
Write-Host "[2/5] Installing dependencies..." -ForegroundColor Yellow
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]" -q 2>$null
Write-Host "         done." -ForegroundColor Green

# 3. Migrate
Write-Host "[3/5] Running migrations..." -ForegroundColor Yellow
& ".\.venv\Scripts\python.exe" manage.py migrate --run-syncdb -q 2>$null
Write-Host "         done." -ForegroundColor Green

# 4. Seed demo data
Write-Host "[4/5] Seeding demo data..." -ForegroundColor Yellow
& ".\.venv\Scripts\python.exe" manage.py seed_demo -q 2>$null
Write-Host "         done." -ForegroundColor Green

# 5. Start server
Write-Host "[5/5] Starting server..." -ForegroundColor Yellow
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  OPEN BROWSER: http://127.0.0.1:8000/" -ForegroundColor White
Write-Host "  Login: admin / admin12345" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

Start-Process "http://127.0.0.1:8000/"
& ".\.venv\Scripts\python.exe" manage.py runserver 0.0.0.0:8000

Pop-Location
