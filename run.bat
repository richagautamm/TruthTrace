@echo off
rem One-click start for Windows: creates the virtual environment on first run, then starts TruthTrace.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Setting up Python environment (first run only)...
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
start "" http://127.0.0.1:8000
.venv\Scripts\python run.py
goto :eof
:error
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
