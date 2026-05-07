# deploy_forex_mt4.ps1
# Run as Administrator on the Windows Server 2022 FXVM.
# Performs Tasks 1-5: find Forex.com MT4 terminal, copy EAs, deploy news monitor,
# register scheduled task, and print verification summary.

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# TASK 1 — Find Forex.com MT4 terminal ID
# ---------------------------------------------------------------------------
Write-Host "`n=== TASK 1: Finding Forex.com MT4 terminal ID ==="

$terminalRoot = "C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal"
$dirs = Get-ChildItem $terminalRoot -Directory

$forexcomTerminalId   = $null
$forexcomTerminalPath = $null

foreach ($dir in $dirs) {
    $originFile = Join-Path $dir.FullName 'origin.txt'
    if (Test-Path $originFile) {
        $originContent = (Get-Content $originFile -Raw).Trim()
        Write-Host "  $($dir.Name) -> $originContent"
        # Match any path that mentions forex.com (case-insensitive)
        if ($originContent -imatch 'forex\.com' -or $originContent -imatch 'forexcom') {
            $forexcomTerminalId   = $dir.Name
            $forexcomTerminalPath = $dir.FullName
        }
    } else {
        Write-Host "  $($dir.Name) -> (no origin.txt)"
    }
}

if (-not $forexcomTerminalId) {
    Write-Error "Could not identify the Forex.com terminal. Check the origin.txt files above."
    exit 1
}

Write-Host ""
Write-Host "Forex.com terminal ID : $forexcomTerminalId"
Write-Host "Forex.com terminal path: $forexcomTerminalPath"

# ---------------------------------------------------------------------------
# TASK 2 — Copy EA files
# ---------------------------------------------------------------------------
Write-Host "`n=== TASK 2: Copying EA files ==="

$srcBase    = 'C:\Users\markt\NNFX\MT4_Q8_Forex'
$expertsDst = Join-Path $forexcomTerminalPath 'MQL4\Experts'
$indicDst   = Join-Path $forexcomTerminalPath 'MQL4\Indicators'

# Ensure destination directories exist
foreach ($d in @($expertsDst, $indicDst)) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-Host "  Created directory: $d"
    }
}

$eaSrc   = Join-Path $srcBase 'q8_ht_vortex.mq4'
$eaDst   = Join-Path $expertsDst 'q8_ht_vortex.mq4'
$indSrc  = Join-Path $srcBase 'WAE_Ext_rebuilt.mq4'
$indDst  = Join-Path $indicDst 'WAE_Ext_rebuilt.mq4'

Copy-Item -Path $eaSrc  -Destination $eaDst  -Force
Write-Host "  Copied EA        : $eaSrc -> $eaDst"

Copy-Item -Path $indSrc -Destination $indDst -Force
Write-Host "  Copied Indicator : $indSrc -> $indDst"

$eaOk  = Test-Path $eaDst
$indOk = Test-Path $indDst
Write-Host "  EA present       : $eaOk"
Write-Host "  Indicator present: $indOk"

# ---------------------------------------------------------------------------
# TASK 3 — Deploy news monitor for Forex.com
# ---------------------------------------------------------------------------
Write-Host "`n=== TASK 3: Deploying q8_forexcom_monitor.py ==="

$srcMonitor = 'C:\Users\Administrator\Desktop\q8_monitor.py'
$dstMonitor = 'C:\Users\Administrator\Desktop\q8_forexcom_monitor.py'
$mt4FilesPath = Join-Path $forexcomTerminalPath 'MQL4\Files\'

$content = Get-Content $srcMonitor -Raw

# 1. Update MT4_FILES_PATH
$content = $content -replace '(?m)^(MT4_FILES_PATH\s*=\s*).*$', "`${1}'$mt4FilesPath'"

# 2. Rename log labels [Q8] -> [Q8-FC]
$content = $content -replace '\[Q8\]', '[Q8-FC]'

# 3. Rename CSV file
$content = $content -replace 'q8_news\.csv', 'q8_forexcom_news.csv'

Set-Content -Path $dstMonitor -Value $content -Encoding UTF8
Write-Host "  Saved : $dstMonitor"
Write-Host "  MT4_FILES_PATH set to: $mt4FilesPath"

# Quick verification — print the relevant lines
Write-Host ""
Write-Host "  --- Verification lines from q8_forexcom_monitor.py ---"
Get-Content $dstMonitor | Select-String 'MT4_FILES_PATH|\[Q8|q8_forexcom_news'

# ---------------------------------------------------------------------------
# TASK 4 — Register Task Scheduler: Q8 ForexCom Monitor
# ---------------------------------------------------------------------------
Write-Host "`n=== TASK 4: Registering scheduled task ==="

$existingTaskName = 'Q8 News Monitor'
$newTaskName      = 'Q8 ForexCom Monitor'
$pythonScript     = $dstMonitor

# Read the existing task to replicate its settings
try {
    $existingTask = Get-ScheduledTask -TaskName $existingTaskName -ErrorAction Stop
    $existingAction   = $existingTask.Actions[0]
    $existingSettings = $existingTask.Settings
    $existingPrincipal = $existingTask.Principal

    # Build new action: same Python executable, new script path
    $newAction = New-ScheduledTaskAction `
        -Execute $existingAction.Execute `
        -Argument "\"$pythonScript\"" `
        -WorkingDirectory (Split-Path $pythonScript)

    # Trigger: at logon of the trader user
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User 'trader'

    # Principal: run as trader
    $principal = New-ScheduledTaskPrincipal `
        -UserId 'trader' `
        -LogonType Interactive `
        -RunLevel Highest

    # Settings cloned from existing task
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit $existingSettings.ExecutionTimeLimit `
        -MultipleInstances $existingSettings.MultipleInstances `
        -RestartCount $existingSettings.RestartCount `
        -RestartInterval $existingSettings.RestartInterval `
        -StartWhenAvailable:$existingSettings.StartWhenAvailable

    # Remove old registration if it exists
    if (Get-ScheduledTask -TaskName $newTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $newTaskName -Confirm:$false
        Write-Host "  Removed existing task: $newTaskName"
    }

    Register-ScheduledTask `
        -TaskName  $newTaskName `
        -Action    $newAction `
        -Trigger   $trigger `
        -Principal $principal `
        -Settings  $settings `
        -Description 'Monitors Forex Factory news for Forex.com MT4 (Q8-FC)' | Out-Null

    Write-Host "  Registered task  : $newTaskName"
    Write-Host "  Script           : $pythonScript"
    Write-Host "  Trigger          : At logon (user: trader)"
    Write-Host "  Executable       : $($existingAction.Execute)"

} catch [Microsoft.PowerShell.Cmdletization.Cim.CimJobException] {
    Write-Warning "Existing task '$existingTaskName' not found. Falling back to defaults."

    # Fallback: use pythonw.exe from common install paths
    $pythonExe = @(
        'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe',
        'C:\Python312\pythonw.exe',
        'C:\Python311\pythonw.exe',
        'C:\Python310\pythonw.exe'
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $pythonExe) { $pythonExe = 'pythonw.exe' }

    $newAction = New-ScheduledTaskAction `
        -Execute $pythonExe `
        -Argument "\"$pythonScript\"" `
        -WorkingDirectory (Split-Path $pythonScript)

    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User 'trader'
    $principal = New-ScheduledTaskPrincipal -UserId 'trader' -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 0) -StartWhenAvailable

    if (Get-ScheduledTask -TaskName $newTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $newTaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName  $newTaskName `
        -Action    $newAction `
        -Trigger   $trigger `
        -Principal $principal `
        -Settings  $settings `
        -Description 'Monitors Forex Factory news for Forex.com MT4 (Q8-FC)' | Out-Null

    Write-Host "  Registered task (fallback defaults): $newTaskName"
}

# ---------------------------------------------------------------------------
# TASK 5 — Verification summary
# ---------------------------------------------------------------------------
Write-Host "`n=== TASK 5: Verification Summary ==="

$taskOk = (Get-ScheduledTask -TaskName $newTaskName -ErrorAction SilentlyContinue) -ne $null

Write-Host ""
Write-Host "  [1] Forex.com terminal ID   : $forexcomTerminalId"
Write-Host "  [1] Forex.com terminal path : $forexcomTerminalPath"
Write-Host ""
Write-Host "  [2] EA copied               : $eaOk  -> $eaDst"
Write-Host "  [2] Indicator copied        : $indOk -> $indDst"
Write-Host ""
Write-Host "  [3] Monitor script saved    : $(Test-Path $dstMonitor)  -> $dstMonitor"
Write-Host "  [3] MT4_FILES_PATH          : $mt4FilesPath"
Write-Host ""
Write-Host "  [4] Scheduled task created  : $taskOk ($newTaskName)"
Write-Host ""

if ($eaOk -and $indOk -and (Test-Path $dstMonitor) -and $taskOk) {
    Write-Host "  ALL TASKS COMPLETED SUCCESSFULLY."
} else {
    Write-Warning "One or more tasks may not have completed. Review the output above."
}
