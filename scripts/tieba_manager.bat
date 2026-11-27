@echo off
cd /d "%~dp0.."
where pythonw >nul 2>nul
if errorlevel 1 (
    echo pythonw not found in PATH.
    pause
    exit /b 1
)
start "" pythonw "scripts\tieba_manager.py"