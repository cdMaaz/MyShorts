@echo off
title MyShorts
color 0A

:: ── Force refresh PATH ────────────────────────────────────────────────────
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH 2^>nul') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "USR_PATH=%%B"
if defined SYS_PATH set "PATH=%SYS_PATH%;%PATH%"
if defined USR_PATH set "PATH=%USR_PATH%;%PATH%"
set "PATH=%PATH%;C:\Program Files\nodejs;C:\ffmpeg\bin;C:\tools\ffmpeg\bin"
set "PATH=%PATH%;C:\Python311;C:\Python311\Scripts;C:\Python312;C:\Python312\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"

echo.
echo  ========================================
echo   MyShorts - Starting
echo  ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 ( echo [ERROR] Python not found. Run INSTALL_WINDOWS.bat first! & pause & exit /b 1 )
ffmpeg -version >nul 2>&1
if errorlevel 1 ( echo [ERROR] FFmpeg not found. Run INSTALL_WINDOWS.bat first! & pause & exit /b 1 )

if not exist uploads mkdir uploads
if not exist output  mkdir output

if not exist frontend\dist\index.html (
    echo  Building frontend first...
    cd frontend
    call npm install --legacy-peer-deps --silent
    call npm run build
    cd ..
)

echo  Server starting at http://localhost:8000
echo  Browser will open automatically...
echo  Press Ctrl+C to stop
echo.

start /b cmd /c "timeout /t 4 /nobreak >nul && start http://localhost:8000"

cd backend
python -m uvicorn app:app --host 0.0.0.0 --port 8000
cd ..

echo.
echo  Server stopped.
pause
