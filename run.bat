@echo off
title Smart Life RGB Controller
cd /d "%~dp0"
python main.py
if %errorlevel% neq 0 (
    echo.
    echo [Ошибка] Произошел сбой при запуске программы.
    pause
)
