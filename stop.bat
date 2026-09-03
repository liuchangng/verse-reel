@echo off
rem stop.bat - stop services by killing the listener on each port in scaffold.cfg
rem Loop-driven: adding a service means adding one line to scaffold.cfg, not editing this file.
rem All output is ASCII English to avoid codepage garbling.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "scaffold.cfg" (
  echo [ERROR] scaffold.cfg not found in project root.
  exit /b 1
)

set "KILLED=0"
for /f "usebackq eol=# tokens=1,2,3 delims=|" %%a in ("scaffold.cfg") do (
  if not "%%c"=="" if not "%%c"=="-" (
    for /f "tokens=5" %%i in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%%c " 2^>nul') do (
      echo [INFO] Stopping %%a - killing PID %%i on port %%c
      taskkill /F /PID %%i >nul 2>&1
      set "KILLED=1"
    )
  )
)

if "!KILLED!"=="0" (
  echo [WARN] No listener found on the configured ports. Nothing stopped.
) else (
  echo [OK] Stopped.
)
endlocal
