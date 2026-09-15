@echo off
REM Start EDNIS Drafter. Run install.bat once first, and make sure
REM ednis\launch_chrome_debug.bat is running with NetSuite + eDesk logged in.
if "%~1"=="hidden" goto :run
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'hidden' -WindowStyle Hidden"
exit /b

:run
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtual environment missing. Run install.bat first. > ednisplus_error.log
    exit /b 1
)

".venv\Scripts\pythonw.exe" app.py
endlocal
