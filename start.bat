@echo off
cd /d "%~dp0"
title Trading bot
call .venv\Scripts\activate.bat
:loop
python bot.py
echo.
echo The bot stopped. Restarting in 10 seconds (close this window to stop it)...
timeout /t 10 >nul
goto loop
