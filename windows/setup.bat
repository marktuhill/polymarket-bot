@echo off
REM One-time setup helper for the Windows VPS deployment.
REM Assumes you've already: installed Python 3.11+, installed and logged into MT5,
REM and cloned this repo. Run this from the repo root.

setlocal

echo === Installing Python dependencies ===
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
python -m pip install -r requirements-mt5.txt
if errorlevel 1 goto :fail

echo.
echo === Checking required environment variables ===
if "%MT5_LOGIN%"=="" (
  echo MT5_LOGIN is NOT set. Set it now ^(then close + reopen this shell^):
  echo   setx MT5_LOGIN 12345678
  goto :env_warn
)
if "%MT5_PASSWORD%"=="" (
  echo MT5_PASSWORD is NOT set. Set it now:
  echo   setx MT5_PASSWORD "your-mt5-password"
  goto :env_warn
)
if "%MT5_SERVER%"=="" (
  echo MT5_SERVER is NOT set. Set it now:
  echo   setx MT5_SERVER "ICMarketsSC-Demo"
  goto :env_warn
)
echo OK  MT5_LOGIN, MT5_PASSWORD, MT5_SERVER all set.

echo.
echo === Running MT5 health check ===
python scripts\mt5_health_check.py
if errorlevel 1 goto :fail

echo.
echo === SETUP COMPLETE ===
echo Next:
echo   1. Trial run:   python scripts\paper_bot.py --broker mt5
echo   2. Schedule:    Task Scheduler -^> Create Task -^> use windows\run_daily.bat
exit /b 0

:env_warn
echo.
echo Set the missing env vars with setx, then open a NEW cmd window and re-run setup.bat.
exit /b 1

:fail
echo.
echo === SETUP FAILED — see error above ===
exit /b 1
