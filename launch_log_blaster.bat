@echo off
REM Opens ComfyUI-LogBlaster in a sleek borderless native app window (Edge/Chrome).
REM Pass a custom port as %1 if ComfyUI is not on 8000 (default: 8000).
set PORT=%1
if "%PORT%"=="" set PORT=8000
set URL=http://127.0.0.1:%PORT%/logblaster

where msedge >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" msedge --app=%URL% --window-size=1360,840
    exit /b 0
)

if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=%URL% --window-size=1360,840
    exit /b 0
)

start "" %URL%
