@echo off
rem Agent OS x PayPal: one-step local start for Windows.
rem Double-click this file, or run  scripts\run_dev.bat  from the project folder.
setlocal
cd /d "%~dp0\.."

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating the virtual environment in .venv ...
    %PY% -m venv .venv || goto :fail
)

echo [2/4] Installing dependencies (first run takes a minute) ...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e ".[dev]" || goto :fail

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo       Created .env from .env.example ^(PAYPAL_ENV=mock, no keys needed^).
)

echo [3/4] Registering PayPal functions (connect time) ...
".venv\Scripts\python.exe" -m cli.main paypal register || goto :fail

echo [4/4] Starting the daemon ...
".venv\Scripts\python.exe" -m cli.main serve
goto :eof

:fail
echo.
echo Something failed. Read the message above, fix it, and run this file again.
pause
exit /b 1
