@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title MOMOKA 起動ツール

set "VENV_DIR=.venv"
set "PYTHON_CMD=py -3.11"
set "START_DIR=%~dp0"
cd /d "%START_DIR%"

:: 管理者権限で実行されているか確認
net session >nul 2>&1
if %errorLevel% == 0 (
    set "ADMIN_MODE=1"
    title [管理者] MOMOKA 起動ツール
) else (
    set "ADMIN_MODE=0"
    title MOMOKA 起動ツール
)

echo ================================
echo        MOMOKA 起動ツール
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

REM 既存 venv が 3.11 以外なら自動で作り直す
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

REM 仮想環境の存在チェック
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

REM 仮想環境のアクティベート
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

REM pip を先に更新して解決器の不具合を減らす
echo [INFO] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [WARN] pip upgrade failed — continuing with existing pip.
)

REM 依存関係のインストール
echo [INFO] Installing dependencies from requirements.txt ...
echo [INFO] First run may take several minutes ^(PyTorch CUDA wheels^).
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] パッケージのインストールに失敗しました。
    echo [ERROR] GPU/CUDA 環境と requirements.txt の torch==2.1.0+cu118 を確認してください。
    pause
    exit /b 1
)
echo [SUCCESS] すべての依存関係が正常にインストールされました。
echo.

REM 任意: YouTube クッキーの存在チェック（無くても起動は続行）
if exist "youtube_cookie.txt" (
    echo [INFO] Found youtube_cookie.txt — music playback will use it.
) else if exist "youtube_cookies.txt" (
    echo [INFO] Found youtube_cookies.txt — music playback will use it.
) else (
    echo [INFO] No YouTube cookie file found ^(optional: place youtube_cookie.txt in project root^).
)

REM YouTube EJS: Deno / Node が無いと signature solving に失敗して無音・format エラーになる
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

REM 同梱BgUtils PO Token ProviderはNode.js 20以上でビルド・実行する
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

REM MOMOKAの起動
echo ================================
echo Starting MOMOKA...
echo ================================
echo.
python main.py
set "MOMOKA_EXIT=!errorlevel!"

REM 終了時の処理
echo.
echo ================================
echo MOMOKA has stopped.
echo ================================
pause
exit /b !MOMOKA_EXIT!
