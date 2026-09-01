@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  py -3 --version >nul 2>&1
  if errorlevel 1 (
    echo Python 3 is required to start CFR.
    pause
    exit /b 1
  )
  py -3 scripts\run_cfr_control.py --open-browser
) else (
  python scripts\run_cfr_control.py --open-browser
)

set "CFR_EXIT=%ERRORLEVEL%"
if not "%CFR_EXIT%"=="0" (
  echo CFR launcher exited with an error.
  pause
)
exit /b %CFR_EXIT%
