@echo off
echo ========================================
echo   古诗词短视频工厂 - 启动脚本
echo ========================================
echo.

:: 检查 Python 环境
echo [1/3] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python 3.11+
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

:: 安装后端依赖
echo [3/3] 安装依赖...
cd server
pip install -r requirements.txt -q
cd ..

cd client
call npm install --silent
cd ..

echo.
echo ========================================
echo   启动服务...
echo ========================================
echo.

:: 启动后端
echo [后端] 启动 FastAPI 服务器 (端口 8000)...
start "后端服务" cmd /k "cd server && python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"

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
echo   按任意键关闭此窗口...
pause >nul
