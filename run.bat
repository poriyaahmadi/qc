@echo off
cd /d "%~dp0"

echo Checking requirements...

where python >nul 2>nul
if errorlevel 1 (
  echo python not found. Install it from https://www.python.org/downloads/ and re-run this script.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg not found. Download it from https://ffmpeg.org/download.html
  echo and add its "bin" folder to your PATH, then re-run this script.
  pause
  exit /b 1
)

if not exist config.json (
  echo No config.json found - creating one from the template.
  copy config.example.json config.json >nul
  echo.
  echo Open config.json and paste your API key before running this again:
  echo   - groq_api_key   ^(free, from https://console.groq.com^)
  echo   - or anthropic_api_key and set qc_provider to "claude"
  echo.
  pause
  exit /b 0
)

if not exist venv (
  echo Setting up a local Python environment ^(one-time, only happens once^)...
  python -m venv venv
)

echo Installing/checking Python packages...
venv\Scripts\pip.exe install -q --upgrade pip
venv\Scripts\pip.exe install -q -r requirements.txt

echo.
echo Starting server...
venv\Scripts\python.exe server.py
pause
