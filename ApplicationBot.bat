@echo off
REM Windows double-click launcher. Sets everything up (virtualenv, dependencies, the
REM automation browser) on first run and starts ApplicationBot; reuses them afterward.
REM Idempotent and safe to re-run (Agent Guideline #8).
setlocal enableextensions
cd /d "%~dp0"

set "PORT=8000"
if not "%~1"=="" set "PORT=%~1"

REM 0. Python 3 is the one prerequisite we can't install for you.
set "PYLAUNCH="
where py >nul 2>nul && set "PYLAUNCH=py -3"
if not defined PYLAUNCH ( where python >nul 2>nul && set "PYLAUNCH=python" )
if not defined PYLAUNCH (
  echo X Python 3 is required but was not found.
  echo   Install it from https://www.python.org/downloads/windows/ ^(check "Add python.exe to PATH"^),
  echo   then double-click this launcher again.
  pause
  exit /b 1
)

set "PY=.venv\Scripts\python.exe"

REM 1. Virtualenv (create once, reuse after).
if not exist "%PY%" (
  echo -^> Creating virtualenv ^(.venv^)...
  %PYLAUNCH% -m venv .venv
)

REM 2. Python dependencies.
echo -^> Installing dependencies...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt

REM 3. Automation browser (idempotent; no-ops when already installed).
echo -^> Ensuring the automation browser ^(Chromium^) is installed...
"%PY%" -m playwright install chromium

REM 4. Claude Code is optional (the free rules engine works without it).
where claude >nul 2>nul && (
  echo -^> Claude Code present - the 'claude-code' engine uses your subscription.
) || (
  echo -^> Claude Code not found. The app will use the free 'rules' engine.
  echo    For Claude-quality tailoring: https://claude.com/product/claude-code
)

REM 5. Readiness report (non-fatal - the in-app "Finish setup" guide covers the rest).
echo -^> Checking readiness...
"%PY%" -m applicationbot.doctor

echo.
echo -^> Opening the ApplicationBot window... (this setup window can be closed once it appears)
REM Launch the standalone native window (WebView2). pythonw.exe = no extra console window.
set "PYW=.venv\Scripts\pythonw.exe"
if exist "%PYW%" (
  start "" "%PYW%" -m applicationbot.app
) else (
  "%PY%" -m applicationbot.app
)
endlocal
