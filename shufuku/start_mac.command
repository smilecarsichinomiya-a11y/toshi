#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "初回セットアップ中です(数分かかります)..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
(sleep 3; open http://127.0.0.1:8100) &
.venv/bin/python -m shufuku
