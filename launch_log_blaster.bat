@echo off
REM Opens ComfyUI-LogBlaster in a sleek borderless native app window (Edge/Chrome).
REM Pass optional port as %1 (default: 8000) and host as %2 (default: 127.0.0.1).
set PORT=%1
if "%PORT%"=="" set PORT=8000
set HOSTNAME=%2
if "%HOSTNAME%"=="" set HOSTNAME=127.0.0.1
set URL=http://%HOSTNAME%:%PORT%/logblaster

where msedge >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" msedge --app=%URL% --window-size=1360,840
    exit /b 0
)

if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=%URL% --window-size=1360,840
    exit /b 0
)

if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files\Microsoft\Edge\Application\msedge.exe" --app=%URL% --window-size=1360,840
    exit /b 0
)

where chrome >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" chrome --app=%URL% --window-size=1360,840
    exit /b 0
)

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=%URL% --window-size=1360,840
    exit /b 0
)

start "" %URL%
