@echo off
REM Roda o app no seu computador para teste: http://127.0.0.1:8100
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt
REM --reload: aplica sozinho as alteracoes no codigo (so para teste; na VPS nao usar)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload --reload-dir app
pause
