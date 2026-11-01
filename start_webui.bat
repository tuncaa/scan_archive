@echo off
setlocal

cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0scan_archive.py"

if not exist "%PYTHON%" (
    echo ERROR: Virtual environment was not found.
    echo Run setup_venv.bat first.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo ERROR: scan_archive.py was not found.
    pause
    exit /b 1
)

echo Starting Web UI at http://127.0.0.1:5000
echo Close this window to stop the Web UI.
"%PYTHON%" "%SCRIPT%" --webui --host 127.0.0.1 --port 5000
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo ERROR: Web UI stopped with exit code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
