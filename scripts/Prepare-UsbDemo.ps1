$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $root "dist"
$target = Join-Path $distRoot "RMcodex-demo"

if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $target | Out-Null

$excludeDirs = @(
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "db-data",
    "media",
    "oldWorkMDprd",
    "dist"
)

$excludeFiles = @(
    ".env",
    "*.pyc",
    "*.m4a",
    "*.pdf",
    "*.dump"
)

& robocopy $root $target /E /XD $excludeDirs /XF $excludeFiles /NFL /NDL /NJH /NJS /NP
$robocopyExitCode = $LASTEXITCODE
if ($robocopyExitCode -gt 7) {
    throw "robocopy failed with exit code $robocopyExitCode"
}

$installersDir = Join-Path $target "installers"
New-Item -ItemType Directory -Force -Path $installersDir | Out-Null
$toolsDir = Join-Path $target "tools"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

Set-Content -Encoding UTF8 -LiteralPath (Join-Path $installersDir "README.txt") -Value @"
If Docker Desktop is not installed on the demo laptop:
1. download Docker Desktop Installer.exe in advance;
2. put the installer into this installers folder;
3. run START_DEMO.bat on the demo laptop.

START_DEMO.bat will offer to open the installer when Docker is missing.
"@

Set-Content -Encoding UTF8 -LiteralPath (Join-Path $toolsDir "README.txt") -Value @"
For remote demo through Cloudflare Tunnel:
1. download cloudflared.exe in advance;
2. put it into this tools folder as cloudflared.exe;
3. run START_REMOTE_DEMO.bat.

Alternative install command on Windows:
winget install --id Cloudflare.cloudflared
"@

$demoReadme = Join-Path $root "docs\demo\README_DEMO.txt"
if (Test-Path -LiteralPath $demoReadme) {
    Copy-Item -LiteralPath $demoReadme -Destination (Join-Path $target "README_DEMO.txt") -Force
}

Write-Host ""
Write-Host "USB demo folder is ready:" -ForegroundColor Green
Write-Host $target
Write-Host ""
Write-Host "Copy RMcodex-demo to the USB drive."
