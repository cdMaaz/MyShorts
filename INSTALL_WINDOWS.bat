@echo off
title MyShorts - Windows Installer
color 0A

echo.
echo  ========================================
echo   MyShorts - Windows Installer
echo  ========================================
echo.

:: ── Force refresh PATH from registry ──────────────────────────────────────
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH 2^>nul') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "USR_PATH=%%B"
if defined SYS_PATH set "PATH=%SYS_PATH%;%PATH%"
if defined USR_PATH set "PATH=%USR_PATH%;%PATH%"
set "PATH=%PATH%;C:\Program Files\nodejs;C:\ffmpeg\bin;C:\tools\ffmpeg\bin"
set "PATH=%PATH%;C:\Python313;C:\Python313\Scripts"
set "PATH=%PATH%;C:\Python311;C:\Python311\Scripts;C:\Python312;C:\Python312\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python313;%LOCALAPPDATA%\Programs\Python\Python313\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"

:: ── Python ─────────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Install from python.org
    pause & exit /b 1
)
python --version
echo [OK] Python found

:: ── Node ───────────────────────────────────────────────────────────────────
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found! Install from nodejs.org then restart PC.
    pause & exit /b 1
)
node --version
echo [OK] Node.js found

:: ── FFmpeg — auto download + install + add to PATH ────────────────────────
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [INFO] FFmpeg not found. Auto-downloading now...
    echo        This may take 1-2 minutes...
    echo.

    :: Use PowerShell to download + extract + add to PATH automatically
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip';" ^
        "$zip = '$env:TEMP\ffmpeg.zip';" ^
        "$dest = 'C:\ffmpeg';" ^
        "Write-Host '  Downloading FFmpeg...';" ^
        "Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing;" ^
        "Write-Host '  Extracting...';" ^
        "Expand-Archive -Path $zip -DestinationPath '$env:TEMP\ffmpeg_extract' -Force;" ^
        "$inner = Get-ChildItem '$env:TEMP\ffmpeg_extract' | Select-Object -First 1;" ^
        "if (Test-Path $dest) { Remove-Item $dest -Recurse -Force };" ^
        "Move-Item $inner.FullName $dest;" ^
        "Write-Host '  Adding to PATH...';" ^
        "$reg = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment';" ^
        "$old = (Get-ItemProperty $reg).Path;" ^
        "if ($old -notlike '*C:\ffmpeg\bin*') { Set-ItemProperty $reg Path ($old + ';C:\ffmpeg\bin') };" ^
        "Write-Host '  Done.'"

    :: Refresh PATH again after install
    for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH 2^>nul') do set "PATH=%%B;%PATH%"
    set "PATH=%PATH%;C:\ffmpeg\bin"

    ffmpeg -version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo [ERROR] Auto-install failed. Do this manually:
        echo.
        echo   1. Open File Explorer, go to C:\
        echo   2. Create a folder called "ffmpeg"
        echo   3. Open the zip you downloaded
        echo   4. Copy the "bin" folder into C:\ffmpeg
        echo      (so you have C:\ffmpeg\bin\ffmpeg.exe)
        echo.
        echo   Then to add to PATH:
        echo   1. Press Win + R, type: sysdm.cpl, press Enter
        echo   2. Click "Advanced" tab
        echo   3. Click "Environment Variables"
        echo   4. Under "System variables", click "Path", then "Edit"
        echo   5. Click "New", type: C:\ffmpeg\bin
        echo   6. Click OK on all windows
        echo   7. Run this installer again
        echo.
        pause & exit /b 1
    )
)
echo [OK] FFmpeg found

:: ── Python packages ────────────────────────────────────────────────────────
echo.
echo  Installing Python packages (3-5 min)...
echo.
pip install fastapi "uvicorn[standard]" python-multipart google-genai faster-whisper yt-dlp opencv-python-headless ultralytics numpy python-dotenv aiofiles httpx
if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed. Make sure you right-clicked "Run as Administrator"
    pause & exit /b 1
)
echo.
echo [OK] Python packages installed

:: ── Frontend ───────────────────────────────────────────────────────────────
echo.
echo  Installing frontend (npm install)...
cd frontend
call npm install --legacy-peer-deps
if errorlevel 1 ( echo [ERROR] npm install failed! & cd .. & pause & exit /b 1 )

echo  Building frontend...
call npm run build
if errorlevel 1 ( echo [ERROR] npm build failed! & cd .. & pause & exit /b 1 )
cd ..
echo [OK] Frontend ready

echo.
echo  ========================================
echo   ALL DONE! Double-click START.bat now.
echo  ========================================
echo.
pause
