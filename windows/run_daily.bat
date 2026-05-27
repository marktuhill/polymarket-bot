@echo off
REM Daily runner wrapper for Windows Task Scheduler.
REM Edit REPO_DIR and PYTHON below if your install paths differ.

set REPO_DIR=C:\polymarket-bot
set PYTHON=python

cd /d "%REPO_DIR%"

REM Append both stdout and stderr to bot.log with a timestamp marker.
echo. >> data\bot.log
echo === %DATE% %TIME% === >> data\bot.log
"%PYTHON%" scripts\paper_bot.py --broker mt5 >> data\bot.log 2>&1
exit /b %ERRORLEVEL%
