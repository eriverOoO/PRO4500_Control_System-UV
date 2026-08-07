@echo off
setlocal
cd /d "%~dp0"

set "MINGW="
for %%D in ("%MSYSTEM_PREFIX%\bin" "C:\msys64\ucrt64\bin" "C:\msys64\mingw64\bin" "C:\msys64\clang64\bin") do (
  if not defined MINGW if exist "%%~D\g++.exe" set "MINGW=%%~D"
)

if not defined MINGW (
  echo [ERROR] MinGW-w64 g++.exe was not found.
  exit /b 1
)

"%MINGW%\g++.exe" -std=c++17 -O2 -Wall -Wextra -municode -mwindows ^
  CheckerboardCalibrationCapturePanel.cpp ^
  -o CheckerboardCalibrationCapturePanel.exe ^
  -lcomctl32 -lshell32

if errorlevel 1 (
  echo [ERROR] Build failed.
  exit /b 1
)

echo Build complete: %CD%\CheckerboardCalibrationCapturePanel.exe
