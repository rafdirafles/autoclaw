@echo off
setlocal
title AutoClaw Services

REM Load API key from system env (set during setup.bat or manually)
if defined AUTOCLAW_API_KEY (
    set "ROUTER_API_KEY=%AUTOCLAW_API_KEY%"
) else (
    set "ROUTER_API_KEY=sk-change-me"
)

:MENU
echo.
echo ============================================
echo    AutoClaw Services Manager
echo    Router:    localhost:31000
echo    Dashboard: localhost:31001
echo ============================================
echo.
echo    [1] Start (background, auto-restart)
echo    [2] Stop
echo    [3] Status
echo    [4] Start (foreground, see logs)
echo    [5] Register accounts
echo    [6] Refresh all tokens now
echo    [0] Exit
echo.
set /p CHOICE="   Select: "

if "%CHOICE%"=="1" (
    echo.
    echo    Starting services in background...
    set "AUTOCLAW_API_KEY=%ROUTER_API_KEY%"
    python "%~dp0runner.py" --daemon
    echo.
    echo    Dashboard: http://localhost:31001
    echo.
    pause
    goto MENU
)
if "%CHOICE%"=="2" (
    echo.
    python "%~dp0runner.py" --stop
    echo.
    pause
    goto MENU
)
if "%CHOICE%"=="3" (
    echo.
    python "%~dp0runner.py" --status
    echo.
    pause
    goto MENU
)
if "%CHOICE%"=="4" (
    echo.
    echo    Starting in foreground (Ctrl+C to stop)...
    echo.
    set "AUTOCLAW_API_KEY=%ROUTER_API_KEY%"
    python "%~dp0runner.py"
    pause
    goto MENU
)
if "%CHOICE%"=="5" (
    echo.
    set /p REG_COUNT="   How many accounts to register? (default 5): "
    if "%REG_COUNT%"=="" set "REG_COUNT=5"
    python "%~dp0register.py" --count %REG_COUNT%
    echo.
    pause
    goto MENU
)
if "%CHOICE%"=="6" (
    echo.
    echo    Triggering refresh via router...
    curl -s -X POST http://localhost:31000/refresh-now
    echo.
    echo    Refresh started. Check dashboard for progress.
    echo.
    pause
    goto MENU
)
if "%CHOICE%"=="0" exit /b 0
echo    Invalid choice.
goto MENU
