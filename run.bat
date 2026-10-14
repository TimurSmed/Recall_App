```bat
@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment .venv was not found.
    echo Create it and install project dependencies first.
    pause
    exit /b 1
)

echo Starting Recall API...
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload

pause
```