@echo off
REM Double-click this file (or run "start.bat" in a terminal) to start the bot.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment...
    python -m venv .venv || (echo. & echo ERROR: Python not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
)

".venv\Scripts\python.exe" -c "import telegram, cv2, pypdf, dotenv, tzdata, apscheduler, playwright" 2>nul
if errorlevel 1 (
    echo Installing packages - this takes a few minutes the first time...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --prefer-binary --timeout 120 --retries 10 --resume-retries 30 -r requirements.txt
    if errorlevel 1 (
        echo.
        echo First attempt failed - often network timeout or antivirus. Trying again in 10 seconds...
        timeout /t 10 /nobreak >nul
        ".venv\Scripts\python.exe" -m pip install --prefer-binary --timeout 120 --retries 10 --resume-retries 30 -r requirements.txt
        if errorlevel 1 (
            echo.
            echo ERROR: package installation failed. Copy the red text above and send it to Claude.
            pause
            exit /b 1
        )
    )
)

if not exist ".venv\browser_installed.txt" (
    echo Installing the hidden browser used to open bank receipt links - one time, about 150 MB...
    ".venv\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 (
        echo.
        echo WARNING: browser install failed - CBE QR receipts can't be checked automatically.
        echo Copy the text above and send it to Claude. The bot will still start.
        timeout /t 5 >nul
    ) else (
        echo ok> ".venv\browser_installed.txt"
    )
)

if not exist ".env" copy ".env.example" ".env" >nul

if not exist "cloudflared.exe" (
    echo Downloading cloudflared for Telegram WebApp dashboard...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'" 2>nul
)

echo Starting the bot... press Ctrl+C to stop.
".venv\Scripts\python.exe" main.py
echo.
echo The bot stopped. If you see an error above, copy it and send it to Claude.
pause
