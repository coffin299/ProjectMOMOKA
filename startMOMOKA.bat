@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title MOMOKA Launcher

set "VENV_DIR=.venv"
set "PYTHON_CMD=py -3.11"
set "START_DIR=%~dp0"
cd /d "%START_DIR%"

:: Check whether the script is running elevated
net session >nul 2>&1
if %errorLevel% == 0 (
    set "ADMIN_MODE=1"
    title [Admin] MOMOKA Launcher
) else (
    set "ADMIN_MODE=0"
    title MOMOKA Launcher
)

echo ================================
echo        MOMOKA Launcher
echo ================================
echo [INFO] Python 3.11 + CUDA torch 2.1 stack
echo.

REM Python 3.11 availability check
echo [INFO] Checking for Python 3.11 interpreter...
%PYTHON_CMD% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11 could not be found.
    echo [ERROR] Install Python 3.11 and ensure `py -3.11` works.
    echo [ERROR] Do not use Python 3.12+ / 3.14 as the project default.
    pause
    exit /b 1
)
set "PY311_VERSION=Unknown"
for /f "tokens=2 delims= " %%A in ('%PYTHON_CMD% --version 2^>nul') do set "PY311_VERSION=%%A"
echo [INFO] Detected Python !PY311_VERSION!

REM Recreate venv automatically if it is not Python 3.11
if exist "%VENV_DIR%\Scripts\python.exe" (
    set "EXISTING_PY_VERSION="
    for /f "tokens=2 delims= " %%A in ('"%VENV_DIR%\Scripts\python.exe" --version 2^>nul') do set "EXISTING_PY_VERSION=%%A"
    if defined EXISTING_PY_VERSION (
        echo [INFO] Existing venv Python: !EXISTING_PY_VERSION!
        echo !EXISTING_PY_VERSION! | find "3.11." >nul
        if errorlevel 1 (
            echo [WARN] .venv is not Python 3.11.x — recreating...
            rmdir /s /q "%VENV_DIR%"
            if exist "%VENV_DIR%" (
                echo [ERROR] Failed to remove old .venv. Close other programs using it and retry.
                pause
                exit /b 1
            )
            echo [SUCCESS] Old virtual environment removed.
        )
    )
)

REM Create virtual environment if missing
if not exist "%VENV_DIR%" (
    echo [INFO] Creating virtual environment in '%VENV_DIR%' folder...
    %PYTHON_CMD% -m venv %VENV_DIR%
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        echo [ERROR] Please check if Python 3.11 is installed correctly.
        pause
        exit /b 1
    )
    echo [SUCCESS] Virtual environment created successfully.
    echo.
) else (
    echo [INFO] Virtual environment already exists.
    echo.
)

REM Activate virtual environment
echo [INFO] Activating virtual environment...
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)
echo [SUCCESS] Virtual environment activated.
echo.

REM Verify active Python version is 3.11.x
set "ACTIVE_PY_VERSION="
for /f "tokens=2 delims= " %%A in ('python --version 2^>nul') do set "ACTIVE_PY_VERSION=%%A"
if not defined ACTIVE_PY_VERSION (
    echo [ERROR] Unable to determine Python version inside virtual environment.
    pause
    exit /b 1
)
echo [INFO] Virtual environment Python version: !ACTIVE_PY_VERSION!
echo !ACTIVE_PY_VERSION! | find "3.11." >nul
if errorlevel 1 (
    echo [ERROR] Virtual environment is not using Python 3.11.x.
    echo [ERROR] Delete the '.venv' folder and re-run this script.
    pause
    exit /b 1
)

REM Upgrade pip first to reduce resolver issues
echo [INFO] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [WARN] pip upgrade failed — continuing with existing pip.
)

REM Install dependencies
echo [INFO] Installing dependencies from requirements.txt ...
echo [INFO] First run may take several minutes ^(PyTorch CUDA wheels^).
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install packages.
    echo [ERROR] Check GPU/CUDA and torch==2.1.0+cu118 in requirements.txt.
    pause
    exit /b 1
)
echo [SUCCESS] All dependencies installed successfully.
echo.

REM Optional YouTube cookie check ^(startup continues either way^)
if exist "youtube_cookie.txt" (
    echo [INFO] Found youtube_cookie.txt — music playback will use it.
) else if exist "youtube_cookies.txt" (
    echo [INFO] Found youtube_cookies.txt — music playback will use it.
) else (
    echo [INFO] No YouTube cookie file found ^(optional: place youtube_cookie.txt in project root^).
)

REM YouTube EJS: without Deno/Node, signature solving fails ^(silent / format errors^)
where deno >nul 2>&1
if errorlevel 1 (
    where node >nul 2>&1
    if errorlevel 1 (
        echo [WARN] Neither Deno nor Node.js found on PATH.
        echo [WARN] YouTube music needs a JS runtime for yt-dlp EJS.
        echo [WARN] Install Deno: https://docs.deno.com/runtime/getting_started/installation/
        echo [WARN] Or Node.js 22+: https://nodejs.org/
    ) else (
        echo [INFO] Node.js found — yt-dlp will use it for YouTube EJS.
    )
) else (
    echo [INFO] Deno found — yt-dlp will use it for YouTube EJS ^(recommended^).
)
echo.

REM Bundled BgUtils PO Token Provider requires Node.js 20+ to build and run
echo [INFO] Checking vendored BgUtils PO Token Provider...
where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js 20+ is required for the bundled PO Token Provider.
    echo [ERROR] Install Node.js from https://nodejs.org/ and retry.
    pause
    exit /b 1
)
set "NODE_MAJOR="
for /f "delims=" %%A in ('node -p "process.versions.node.split('.')[0]" 2^>nul') do set "NODE_MAJOR=%%A"
if not defined NODE_MAJOR (
    echo [ERROR] Unable to determine the Node.js version.
    pause
    exit /b 1
)
if !NODE_MAJOR! LSS 20 (
    echo [ERROR] Node.js 20+ is required. Detected major version: !NODE_MAJOR!
    pause
    exit /b 1
)
if not exist "third_party\bgutil-ytdlp-pot-provider\server\package-lock.json" (
    echo [ERROR] Vendored BgUtils Provider source is missing.
    echo [ERROR] Expected: third_party\bgutil-ytdlp-pot-provider\server\package-lock.json
    pause
    exit /b 1
)
if not exist "third_party\bgutil-ytdlp-pot-provider\server\build\main.js" (
    echo [INFO] Installing and building BgUtils Provider v1.3.1...
    pushd "third_party\bgutil-ytdlp-pot-provider\server"
    call npm ci
    if errorlevel 1 (
        popd
        echo [ERROR] BgUtils Provider npm install failed.
        pause
        exit /b 1
    )
    call npx tsc
    if errorlevel 1 (
        popd
        echo [ERROR] BgUtils Provider TypeScript build failed.
        pause
        exit /b 1
    )
    popd
)
echo [SUCCESS] BgUtils PO Token Provider is ready.
echo.

REM Host Electron GUI ^(skip when already built^)
echo [INFO] Checking host Electron GUI ^(gui-electron^)...
if not exist "gui-electron\package.json" (
    echo [WARN] gui-electron\package.json not found — host GUI will be skipped.
) else if exist "gui-electron\dist\index.html" (
    echo [INFO] gui-electron dist already exists — skipping build.
) else (
    echo [INFO] Building host Electron GUI ^(first run^)...
    pushd "gui-electron"
    if exist "package-lock.json" (
        call npm ci
    ) else (
        call npm install
    )
    if errorlevel 1 (
        popd
        echo [WARN] gui-electron npm install failed — Bot will start without Electron GUI.
        goto gui_electron_done
    )
    call npm run build
    if errorlevel 1 (
        popd
        echo [WARN] gui-electron build failed — Bot will start without Electron GUI.
        goto gui_electron_done
    )
    popd
    if exist "gui-electron\dist\index.html" (
        echo [SUCCESS] Host Electron GUI is ready.
    ) else (
        echo [WARN] gui-electron build finished but dist\index.html is missing.
    )
)
:gui_electron_done
echo.

REM Start MOMOKA
echo ================================
echo Starting MOMOKA...
echo ================================
echo.
python main.py
set "MOMOKA_EXIT=!errorlevel!"

REM Shutdown footer
echo.
echo ================================
echo MOMOKA has stopped.
echo ================================
pause
exit /b !MOMOKA_EXIT!
