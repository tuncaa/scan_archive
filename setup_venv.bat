@echo off
setlocal

cd /d "%~dp0"
set "VENV_DIR=%~dp0.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "TESSDATA=C:\Program Files\Tesseract-OCR\tessdata"

where py >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python launcher "py" was not found.
    echo Install Python 3.10 or later and enable the Python launcher.
    exit /b 1
)

if not exist "%PYTHON%" (
    echo Creating virtual environment: "%VENV_DIR%"
    py -3 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to create the virtual environment.
        exit /b 1
    )
) else (
    echo Virtual environment already exists: "%VENV_DIR%"
)

echo Upgrading pip...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip.
    exit /b 1
)

echo Installing Python modules...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo ERROR: Failed to install Python modules.
    exit /b 1
)

echo Verifying PyMuPDF...
"%PYTHON%" -c "import pymupdf; print('PyMuPDF:', pymupdf.__version__)"
if errorlevel 1 (
    echo ERROR: PyMuPDF could not be imported.
    exit /b 1
)

if not exist "%TESSDATA%\jpn.traineddata" (
    echo WARNING: Tesseract Japanese data was not found:
    echo          "%TESSDATA%\jpn.traineddata"
)
if not exist "%TESSDATA%\eng.traineddata" (
    echo WARNING: Tesseract English data was not found:
    echo          "%TESSDATA%\eng.traineddata"
)

echo.
echo Setup completed.
echo Run with:
echo   "%PYTHON%" "%~dp0scan_archive.py"
echo Search with:
echo   "%PYTHON%" "%~dp0scan_archive.py" --search "keyword"
exit /b 0
