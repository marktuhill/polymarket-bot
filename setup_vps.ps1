# Crypto Range Monitor - one-shot VPS setup (Windows PowerShell)
# Run from inside the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\setup_vps.ps1
# Installs the dependency, captures your Telegram credentials into .env,
# sends a Telegram test message, then runs a console test scan.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "=== Crypto Range Monitor setup ===" -ForegroundColor Cyan

# 1) Find Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Host "Python not found." -ForegroundColor Red
    Write-Host "Install Python 3 from https://www.python.org/downloads/ and tick"
    Write-Host "'Add python.exe to PATH' during install, then re-run this script."
    exit 1
}
$exe = $python.Source
Write-Host "Using Python: $exe"
& $exe --version

# 2) Dependency
Write-Host "`nInstalling 'requests'..."
& $exe -m pip install --upgrade pip --quiet
& $exe -m pip install requests --quiet

# 3) Credentials -> .env (kept local, never committed)
$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Host "`nEnter your Telegram credentials (stored only in .env on this machine):"
    $token = Read-Host "TELEGRAM_BOT_TOKEN"
    $chat  = Read-Host "TELEGRAM_CHAT_ID"
    Set-Content -Path $envPath -Value "TELEGRAM_BOT_TOKEN=$token`nTELEGRAM_CHAT_ID=$chat" -Encoding ascii
    Write-Host ".env created."
} else {
    Write-Host "`n.env already exists - leaving it unchanged."
}

# Read back token/chat for the live Telegram check
$map = @{}
foreach ($line in Get-Content $envPath) {
    if ($line -match '^\s*([^#=]+)=(.*)$') { $map[$matches[1].Trim()] = $matches[2].Trim() }
}
$token = $map['TELEGRAM_BOT_TOKEN']
$chat  = $map['TELEGRAM_CHAT_ID']

# 4) Live Telegram check (confirms credentials immediately)
if ($token -and $chat) {
    Write-Host "`nSending a Telegram test message..."
    try {
        Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -Body @{ chat_id = $chat; text = "Crypto Range Monitor: setup test OK." } | Out-Null
        Write-Host "Sent - check your Telegram chat." -ForegroundColor Green
    } catch {
        Write-Host "Telegram test FAILED: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "Double-check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env"
    }
}

# 5) Console test scan (no Telegram alerts are sent by --test)
Write-Host "`nRunning a one-off scan (--test). This fetches pairs and prints detected ranges..." -ForegroundColor Cyan
& $exe crypto_range_monitor.py --test

Write-Host "`nDone. If the ranges above look sane, enable autostart:" -ForegroundColor Green
Write-Host "  right-click PowerShell -> Run as administrator, then:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\register_task.ps1"
