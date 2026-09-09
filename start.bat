@echo off
echo ========================================
echo   Poem Video Factory - Startup Script
echo ========================================
echo.

:: Check uv (backend Python is managed by uv; no global Python required)
echo [1/3] Checking uv...
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv not found. Install it first (winget install astral-sh.uv, or https://docs.astral.sh/uv/)
    pause
    exit /b 1
)

:: Check Node.js
echo [2/3] Checking Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Node.js not found. Install Node.js 18+ first.
    pause
    exit /b 1
)

:: Sync backend deps (managed by uv; based on server/pyproject.toml + server/uv.lock)
echo [3/3] Syncing dependencies...
cd server
uv sync
cd ..

cd client
call npm install --silent
cd ..

echo.
echo ========================================
echo   Starting services...
echo ========================================
echo.

:: Start backend (uv run inside server/.venv)
echo [backend] Starting FastAPI server (port 8000)...
start "backend" cmd /k "cd server && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"

:: Wait for backend to boot
timeout /t 3 /nobreak >nul

:: Start frontend
echo [frontend] Starting Vite dev server (port 5173)...
start "frontend" cmd /k "cd client && npm run dev"

echo.
echo ========================================
echo   Services started!
echo ========================================
echo.
echo   backend:  http://localhost:8000
echo   frontend: http://localhost:5173
echo   API docs: http://localhost:8000/docs
echo.
echo   Tip: for CosyVoice high-quality TTS, run  uv sync --extra cosyvoice  first
echo.
echo   Press any key to close this window...
pause >nul
