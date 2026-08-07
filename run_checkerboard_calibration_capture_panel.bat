@echo off
setlocal

set "ROOT=%~dp0"
set "APP=%ROOT%CheckerboardCalibrationCapturePanel.exe"

if not exist "%APP%" (
  echo Checkerboard capture panel was not found. Build it first:
  echo   build_checkerboard_calibration_capture_panel.bat
  pause
  exit /b 1
)

start "" "%APP%"
