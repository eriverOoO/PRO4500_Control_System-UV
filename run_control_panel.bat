@echo off
setlocal

set "ROOT=%~dp0"
set "APP=%ROOT%StructuredLightControlPanel.exe"

if not exist "%APP%" (
  echo Control panel app was not found:
  echo %APP%
  echo.
  echo Build it first:
  echo   build_native_control_panel.bat
  pause
  exit /b 1
)

start "" "%APP%"
exit /b 0
