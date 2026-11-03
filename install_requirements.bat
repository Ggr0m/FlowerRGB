@echo off
title Установка зависимостей Smart Life RGB Controller
cd /d "%~dp0"
echo Установка необходимых библиотек (tinytuya, pystray, customtkinter, pillow)...
pip install -r requirements.txt
echo.
echo Все библиотеки успешно установлены!
pause
