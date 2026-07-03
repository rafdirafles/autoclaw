@echo off
setlocal
title AutoClaw Setup

echo.
echo ============================================
echo    AutoClaw Setup — First Time Install
echo ============================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.13+ from https://python.org
    echo        Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [1/4] Python found:
python --version

REM Install dependencies
echo.
echo [2/4] Installing dependencies...
pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

REM Create config.json if not exists
if not exist "%~dp0config.json" (
    echo.
    echo [3/4] Creating config.json...
    echo {> "%~dp0config.json"
    echo   "password": "",>> "%~dp0config.json"
    echo   "batch_size": 5,>> "%~dp0config.json"
    echo   "email_file": "email.txt",>> "%~dp0config.json"
    echo   "accounts_file": "accounts.json",>> "%~dp0config.json"
    echo   "tokens_file": "tokens.txt">> "%~dp0config.json"
    echo }>> "%~dp0config.json"
    echo       Config created.
) else (
    echo.
    echo [3/4] config.json already exists — skipping.
)

REM Create empty files if not exists
if not exist "%~dp0email.txt" type nul > "%~dp0email.txt"
if not exist "%~dp0accounts.json" echo [] > "%~dp0accounts.json"
if not exist "%~dp0tokens.txt" type nul > "%~dp0tokens.txt"

REM Set API key env var
echo.
echo [4/4] Setting up API key...
set /p API_KEY="   Enter your router API key (or press Enter for default 'sk-change-me'): "
if "%API_KEY%"=="" set "API_KEY=sk-change-me"

REM Save to .env file (local, gitignored)
echo AUTOCLAW_API_KEY=%API_KEY%> "%~dp0.env"
echo       .env file created.

REM Also set system env var (for non-router usage)
setx AUTOCLAW_API_KEY "%API_KEY%" >nul 2>&1
echo       System env var saved.

echo.
echo ============================================
echo    Setup Complete!
echo ============================================
echo.
echo    Next steps:
echo    1. Add emails to email.txt (format: email:password)
echo    2. Run: python register.py --count 5
echo    3. Run: start.bat
echo.
pause
