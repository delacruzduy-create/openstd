@echo off
setlocal
cd /d "%~dp0"
title OpenSTD Test - no cerrar esta ventana mientras se usa
if not exist "datos" mkdir "datos"

rem ---- Python: el portable del pendrive o el instalado en la PC
set "PY="
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto sin_python

rem ---- Permisos de administrador: hacen falta para leer los discos
net session >nul 2>&1
if not errorlevel 1 goto con_permisos
echo Pidiendo permisos de administrador...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop; exit 0 } catch { exit 1 }"
if not errorlevel 1 exit /b 0
echo.
echo No se dieron permisos de administrador: la lectura de discos va a quedar sin hacer.
echo.

:con_permisos
%PY% -c "import psutil, reportlab, wgpu, glfw" >nul 2>&1
if not errorlevel 1 goto arrancar
echo Instalando las librerias que faltan...
%PY% -m pip install --user -r requirements.txt
%PY% -c "import psutil, reportlab, wgpu, glfw" >nul 2>&1
if errorlevel 1 goto sin_librerias

:arrancar
%PY% -m openstd %*
if errorlevel 1 pause
exit /b 0

:sin_python
echo.
echo  No hay Python en esta PC.
echo  Opcion 1: ejecuta PREPARAR_PENDRIVE.bat una sola vez, en una PC con internet.
echo  Opcion 2: instala Python desde https://www.python.org/downloads/
echo.
pause
exit /b 1

:sin_librerias
echo.
echo  No se pudieron instalar las librerias. Ejecuta PREPARAR_PENDRIVE.bat con internet.
echo.
pause
exit /b 1
