@echo off
chcp 65001 > nul
cd /d %~dp0
if not exist .venv (
  echo 初回セットアップ中です(数分かかります)...
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env copy .env.example .env > nul
start "" http://127.0.0.1:8100
.venv\Scripts\python -m shufuku
pause
