@echo off
echo ========================================
echo   古诗词短视频工厂 - 启动脚本
echo ========================================
echo.

:: 检查 uv 环境（后端 Python 由 uv 托管，无需全局 python）
echo [1/3] 检查 uv 环境...
where uv >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 uv，请先安装 uv（winget install astral-sh.uv 或 https://docs.astral.sh/uv/）
    pause
    exit /b 1
)

:: 检查 Node.js 环境
echo [2/3] 检查 Node.js 环境...
node --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Node.js，请先安装 Node.js 18+
    pause
    exit /b 1
)

:: 安装/同步后端依赖（uv 管理，基于 server/pyproject.toml + server/uv.lock）
echo [3/3] 同步依赖...
cd server
uv sync
cd ..

cd client
call npm install --silent
cd ..

echo.
echo ========================================
echo   启动服务...
echo ========================================
echo.

:: 启动后端（uv run 在 server/.venv 内运行）
echo [后端] 启动 FastAPI 服务器 (端口 8000)...
start "后端服务" cmd /k "cd server && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"

:: 等待后端启动
timeout /t 3 /nobreak >nul

:: 启动前端
echo [前端] 启动 Vite 开发服务器 (端口 5173)...
start "前端服务" cmd /k "cd client && npm run dev"

echo.
echo ========================================
echo   服务已启动！
echo ========================================
echo.
echo   后端: http://localhost:8000
echo   前端: http://localhost:5173
echo   API 文档: http://localhost:8000/docs
echo.
echo   提示: 如需启用 CosyVoice 高质量 TTS，先执行  uv sync --extra cosyvoice
echo.
echo   按任意键关闭此窗口...
pause >nul
