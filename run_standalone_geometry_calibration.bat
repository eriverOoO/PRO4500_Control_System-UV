@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-pc\Scripts\python.exe" (
  echo Python environment not found. Run prepare_pc_python_env.ps1 first.
  pause
  exit /b 1
)
".venv-pc\Scripts\python.exe" "standalone_geometry_calibration\app.py"
if errorlevel 1 pause
