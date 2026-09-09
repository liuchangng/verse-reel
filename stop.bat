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
      rem /T tree-kill is mandatory: uvicorn --reload spawns a child that
      rem inherits the listening socket. Killing only the parent PID leaves
      rem the child alive and the port still bound (2026-09-09 incident).
      taskkill /F /T /PID %%i >nul 2>&1
      set "KILLED=1"
    )
  )
)

rem Verify ports are actually released (a surviving child keeps the port bound).
set "STUCK=0"
for /f "usebackq eol=# tokens=1,2,3 delims=|" %%a in ("scaffold.cfg") do (
  if not "%%c"=="" if not "%%c"=="-" (
    netstat -ano | findstr "LISTENING" | findstr ":%%c " >nul 2>&1 && (
      echo [ERROR] Port %%c is STILL in use after kill. Check manually: netstat -ano ^| findstr ":%%c"
      set "STUCK=1"
    )
  )
)

if "!KILLED!"=="0" (
  echo [WARN] No listener found on the configured ports. Nothing stopped.
) else (
  if "!STUCK!"=="1" (
    exit /b 1
  ) else (
    echo [OK] Stopped. All ports released.
  )
)
endlocal
