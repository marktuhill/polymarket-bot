# Register the monitor to run at VPS startup and survive reboots.
# Run in an ELEVATED PowerShell (Run as administrator):
#   powershell -ExecutionPolicy Bypass -File .\register_task.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Must be Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Please run this as Administrator (right-click PowerShell -> Run as administrator)." -ForegroundColor Red
    exit 1
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { Write-Host "Python not found in PATH." -ForegroundColor Red; exit 1 }
$exe = $python.Source
$script = Join-Path $PSScriptRoot "crypto_range_monitor.py"

$taskName = "CryptoRangeMonitor"
$action  = New-ScheduledTaskAction -Execute $exe -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
# S4U = "run whether the user is logged on or not" without storing a password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettings -StartWhenAvailable `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Scheduled task '$taskName' registered - runs at every startup." -ForegroundColor Green
Write-Host "Start it now (no reboot needed):" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $taskName"
Write-Host "Check it later with:  Get-ScheduledTask -TaskName $taskName ; or the Task Scheduler GUI"
Write-Host "Logs: $((Join-Path $PSScriptRoot 'crypto_range_monitor.log'))"
