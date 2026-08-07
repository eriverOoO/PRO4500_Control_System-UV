@echo off
setlocal

set "ROOT=%~dp0"
set "PY=%ROOT%.venv-pc\Scripts\python.exe"
set "SESSION=%ROOT%captures\checkerboard_calibration_session"

if not exist "%PY%" (
  echo Python runtime was not found: %PY%
  pause
  exit /b 1
)

if exist "%SESSION%\session_manifest.json" (
  echo Session already exists: %SESSION%
  echo Use capture_checkerboard_calibration_pose.bat to record a pose.
  pause
  exit /b 1
)

"%PY%" "%ROOT%checkerboard_calibration_capture.py" setup ^
  --session "%SESSION%" ^
  --patterns "%ROOT%generated_patterns_centered" ^
  --camera-config "%ROOT%camera_config.json"

if errorlevel 1 pause
