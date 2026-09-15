@echo off
REM One-time setup: install Python if missing, create the virtual environment,
REM install dependencies, and playwright's browser binaries aren't needed
REM (this connects to the user's own Chrome over CDP, same as EDNIS).
setlocal
cd /d "%~dp0"

REM --- Python -------------------------------------------------------------
set PY=
where py >nul 2>nul && set PY=py
if not defined PY ( where python >nul 2>nul && set PY=python )

if not defined PY (
    echo Python not found.
    where winget >nul 2>nul
    if %ERRORLEVEL%==0 (
        echo Installing Python 3.12 via winget...
        winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
        echo.
        echo Python installed. CLOSE this window, open a new one, and run install.bat again
        echo so the updated PATH takes effect.
        exit /b 0
    ) else (
        echo winget is not available. Install Python 3.10+ manually from:
        echo     https://www.python.org/downloads/windows/
        echo Tick "Add python.exe to PATH" in the installer, then run install.bat again.
        exit /b 1
    )
)

echo Using Python: %PY%
%PY% --version

REM --- venv + dependencies ---------------------------------------------
if not exist ".venv" (
    echo Creating virtual environment...
    %PY% -m venv .venv
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if not exist ".env" (
    echo Creating .env from .env.example — add your Gemini API key to it.
    copy /y ".env.example" ".env" >nul
)

echo.
echo Done. Next:
echo   1. Add your GEMINI_API_KEY to .env (get one free at aistudio.google.com)
echo   2. Run ednis\launch_chrome_debug.bat and log into NetSuite + eDesk
echo      (this tool shares that same Chrome window with EDNIS)
echo   3. Run run.bat to start EDNIS Drafter
endlocal
