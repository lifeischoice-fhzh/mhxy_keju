@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Project venv not found:
    echo   %PY%
    echo Create it first, then run this script again.
    pause
    exit /b 1
)
"%PY%" "%~dp0package.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [FAILED] exit code %RC%
) else (
    echo [OK]
)
if "%~1"=="" pause
exit /b %RC%
