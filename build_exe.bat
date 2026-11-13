@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing/verifying build dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo Building pyKVM.exe...
".venv\Scripts\python.exe" build_exe.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Successfully built dist\pyKVM.exe
) else (
    echo.
    echo Build failed with error code %ERRORLEVEL%
)
pause
