@echo off
setlocal

cd /d "%~dp0"

echo Processing scan folder before shutdown...
call "%~dp0process_scan.bat"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo ERROR: Scan processing failed with exit code %EXIT_CODE%.
    echo Shutdown was cancelled.
    exit /b %EXIT_CODE%
)

echo Scan processing completed.
echo Starting Web UI and opening the browser...
start "Scan Archive Web UI" /min cmd /c call "%~dp0start_webui.bat"
echo Shutting down this computer in 60 seconds...
shutdown /s /t 60 /c "Scan processing completed."
if errorlevel 1 (
    echo ERROR: Failed to schedule shutdown.
    exit /b 1
)

echo To cancel the scheduled shutdown, run: shutdown /a
exit /b 0
