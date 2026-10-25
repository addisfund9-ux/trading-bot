@echo off
cd /d "%~dp0"
echo Installing the trading bot from the local "wheels" folder (no internet needed)...
if not exist wheels (
  echo The "wheels" folder is missing. Use install.bat instead.
  pause
  exit /b 1
)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install --no-index --find-links wheels -r requirements.txt
if errorlevel 1 (
  echo.
  echo Install failed. Take a screenshot of this window and send it to Claude.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
echo.
echo ALL DONE. Next: open .env in Notepad, paste your Telegram token, save, then double-click start.bat.
pause
