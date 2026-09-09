@echo off
setlocal enabledelayedexpansion

set ROOT_DIR=%~dp0
set BACKEND_DIR=%ROOT_DIR%server
set FRONTEND_DIR=%ROOT_DIR%client
set RUN_DIR=%ROOT_DIR%.run

set BACKEND_PORT=8000
set FRONTEND_PORT=5173

echo ========================================
echo   Poem Video Factory - Startup Script
echo ========================================
echo.

:: Backend Python: use the pre-installed venv directly (no uv check needed).
if exist "%BACKEND_DIR%\.venv\Scripts\python.exe" (
    set PYTHON_EXE=%BACKEND_DIR%\.venv\Scripts\python.exe
) else (
    echo [WARN] %BACKEND_DIR%\.venv not found.
    echo [WARN] Install dependencies first, then re-run this script:
    echo           cd server ^&^& uv sync
    pause
    exit /b 1
)

if not exist "%RUN_DIR%" mkdir "%RUN_DIR%"

> "%RUN_DIR%\backend.bat" (
    echo @echo off
    echo cd /d "%BACKEND_DIR%"
    echo "%PYTHON_EXE%" -m uvicorn app.main:app --reload --host 0.0.0.0 --port %BACKEND_PORT%
    echo pause
)

> "%RUN_DIR%\frontend.bat" (
    echo @echo off
    echo cd /d "%FRONTEND_DIR%"
    echo npm run dev
    echo pause
)

start "poem-backend" cmd /k "%RUN_DIR%\backend.bat"
echo [start] backend  http://localhost:%BACKEND_PORT%

timeout /t 3 /nobreak >nul

start "poem-frontend" cmd /k "%RUN_DIR%\frontend.bat"
echo [start] frontend http://localhost:%FRONTEND_PORT%

echo.
echo   API docs: http://localhost:%BACKEND_PORT%/docs
echo   Tip: for CosyVoice TTS, run  uv sync --extra cosyvoice  first
echo.
echo   Press any key to close this window...
pause
endlocal
