@echo off
cd /d "%~dp0"
echo Setting up the trading bot...
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not installed. Get it from https://www.python.org/downloads/
  echo During install, tick "Add python.exe to PATH". Then run this file again.
  pause
  exit /b 1
)
python -m venv .venv
call .venv\Scripts\activate.bat
pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
  echo.
  echo Install failed. Take a screenshot of this window and send it to Claude.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
echo.
echo Done. Now open the ".env" file in Notepad, paste your two keys, save it,
echo and double-click start.bat.
pause
