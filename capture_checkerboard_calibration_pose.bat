@echo off
setlocal

set "ROOT=%~dp0"
set "PY=%ROOT%.venv-pc\Scripts\python.exe"
set "SESSION=%ROOT%captures\checkerboard_calibration_session"
set "POSE_ID=%~1"

if not exist "%PY%" (
  echo Python runtime was not found: %PY%
  pause
  exit /b 1
)

if not exist "%SESSION%\session_manifest.json" (
  echo Session was not prepared. Run setup_checkerboard_calibration_session.bat first.
  pause
  exit /b 1
)

if "%POSE_ID%"=="" set /p "POSE_ID=Pose ID (for example p01_center_z00): "
if "%POSE_ID%"=="" (
  echo Pose ID is required.
  pause
  exit /b 1
)

"%PY%" "%ROOT%checkerboard_calibration_capture.py" capture ^
  --session "%SESSION%" ^
  --pose-id "%POSE_ID%" ^
  --controller "%ROOT%structured_light_pc_controller.py"

if errorlevel 1 pause
