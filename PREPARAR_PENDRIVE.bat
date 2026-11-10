@echo off
setlocal
cd /d "%~dp0"
title OpenSTD Test - Preparar el pendrive
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\preparar_portable.ps1"
echo.
pause
