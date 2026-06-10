#Requires -Version 5.1
param(
    [switch]$IncludePostgres
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DistDir = Join-Path (Join-Path $ProjectRoot "dist") "RMcodex-viewer"
$BuildDir = Join-Path (Join-Path $ProjectRoot "build") "pyinstaller"

Write-Host "=== RMcodex Viewer Builder ===" -ForegroundColor Cyan

# 0. Find venv Python
$VenvPython = Join-Path (Join-Path $ProjectRoot ".venv-test") (Join-Path "Scripts" "python.exe")
if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: venv python not found at $VenvPython" -ForegroundColor Red
    exit 1
}

# 1. Collectstatic
Write-Host "[1/4] Collecting static..." -ForegroundColor Yellow
Push-Location $ProjectRoot
try {
    $env:DJANGO_SETTINGS_MODULE = "rehab_center.settings_viewer"
    $env:VIEWER_DB = "sqlite"
    & $VenvPython manage.py collectstatic --no-input --clear 2>&1
    if (-not $?) { throw "collectstatic failed" }
} finally { Pop-Location }

# 2. Clean
Write-Host "[2/4] Cleaning..." -ForegroundColor Yellow
if (Test-Path $DistDir) { Remove-Item -Recurse -Force $DistDir }
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
if (Test-Path (Join-Path $ProjectRoot "RMcodex-viewer.spec")) { Remove-Item -Force (Join-Path $ProjectRoot "RMcodex-viewer.spec") }

# 3. PyInstaller
Write-Host "[3/4] Running PyInstaller..." -ForegroundColor Yellow

$PyArgs = @(
    "scripts\launcher.py"
    "--name=RMcodex"
    "--onedir"
    "--clean"
    "--noconfirm"
    "--log-level=WARN"
    "--distpath=$DistDir"
    "--workpath=$BuildDir"
    "--add-data=templates;templates"
    "--add-data=staticfiles;staticfiles"
    "--add-data=static;static"
    "--add-data=rehab_center;rehab_center"
    "--add-data=operations;operations"
    "--add-data=manage.py;."
    "--hidden-import=ninja"
    "--hidden-import=django_htmx"
    "--hidden-import=whitenoise"
    "--hidden-import=reportlab"
    "--hidden-import=django_tasks"
    "--hidden-import=psycopg"
    "--hidden-import=psycopg.types"
    "--hidden-import=asgiref"
    "--hidden-import=sqlparse"
    "--collect-submodules=operations.migrations"
    "--collect-data=operations"
    "--collect-submodules=django.core.management"
    "--collect-submodules=django.contrib.admin.management"
    "--collect-submodules=django.contrib.auth.management"
)

& $VenvPython -m PyInstaller @PyArgs 2>&1

if (-not $?) { throw "PyInstaller failed" }

# 4. PostgreSQL (optional)
if ($IncludePostgres) {
    Write-Host "[4/4] Downloading portable PostgreSQL..." -ForegroundColor Yellow
    $PgDir = Join-Path (Join-Path (Join-Path $DistDir "RMcodex") "_internal") "pgsql"
    $PgUrl = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"
    $PgZip = Join-Path $env:TEMP "postgresql-portable.zip"
    try {
        Invoke-WebRequest -Uri $PgUrl -OutFile $PgZip -UseBasicParsing
        Expand-Archive -Path $PgZip -DestinationPath $PgDir -Force
        $Nested = Get-ChildItem $PgDir -Directory | Select-Object -First 1
        if ($Nested) {
            Get-ChildItem $Nested.FullName | Move-Item -Destination $PgDir -Force
            Remove-Item -Recurse -Force $Nested.FullName
        }
        Write-Host "PostgreSQL bundled" -ForegroundColor Green
    } catch {
        Write-Host "PostgreSQL download failed: $_" -ForegroundColor Red
    }
} else {
    Write-Host "[4/4] Skipped PostgreSQL (use -IncludePostgres)" -ForegroundColor Yellow
}

# Done
$Exe = Join-Path (Join-Path $DistDir "RMcodex") "RMcodex.exe"
$LauncherBat = Join-Path $DistDir "START_RM.bat"
@(
    "@echo off"
    "chcp 65001 >nul"
    "cd /d ""%~dp0RMcodex"""
    "start """" ""RMcodex.exe"""
    ""
) | Set-Content -LiteralPath $LauncherBat -Encoding UTF8
if (Test-Path $Exe) {
    $SizeInMB = [math]::Round((Get-ChildItem -Recurse (Split-Path $Exe -Parent) | Measure-Object Length -Sum).Sum / 1MB, 0)
    Write-Host "EXE: $Exe" -ForegroundColor Green
    Write-Host "Launcher: $LauncherBat" -ForegroundColor Green
    Write-Host "Size: ~$SizeInMB MB" -ForegroundColor Green
} else {
    Write-Host "BUILD FAILED" -ForegroundColor Red
}
