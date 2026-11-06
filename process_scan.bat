@echo off
setlocal

cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0scan_archive.py"

if not exist "%PYTHON%" (
    echo ERROR: Virtual environment was not found.
    echo Run setup_venv.bat first.
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo ERROR: scan_archive.py was not found.
    exit /b 1
)

echo Processing scan folder...
"%PYTHON%" "%SCRIPT%" --config "%~dp0config.json"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo ERROR: Scan processing failed with exit code %EXIT_CODE%.
) else (
    echo Scan processing completed.
)

exit /b %EXIT_CODE%
