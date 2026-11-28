@echo off
cd /d %~dp0
set PY=py -3
where py >nul 2>nul || set PY=python
if exist .venv\ok.txt goto run
echo First-time setup. Please wait a few minutes. Do not close this window.
%PY% -m venv .venv
if errorlevel 1 goto nopy
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo ok> .venv\ok.txt
:run
if not exist .env copy .env.example .env > nul
start "" http://127.0.0.1:8100
.venv\Scripts\python -m shufuku
pause
exit /b 0
:nopy
echo Python was not found. Please install Python from https://www.python.org/downloads/
echo During install, check the box: Add python.exe to PATH
pause
exit /b 1
:fail
echo Setup failed. Check your internet connection and try again.
pause
exit /b 1
