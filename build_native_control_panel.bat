@echo off
setlocal
cd /d "%~dp0"

set "MINGW="
for %%D in ("%MSYSTEM_PREFIX%\bin" "C:\msys64\ucrt64\bin" "C:\msys64\mingw64\bin" "C:\msys64\clang64\bin") do (
  if not defined MINGW if exist "%%~D\g++.exe" set "MINGW=%%~D"
)

if not defined MINGW (
  for %%G in (g++.exe) do set "GXX=%%~$PATH:G"
  if defined GXX for %%D in ("%GXX%") do set "MINGW=%%~dpD"
)

if not defined MINGW (
  echo [ERROR] MinGW-w64 g++.exe was not found.
  echo Install MSYS2 MinGW-w64, or set MINGW to the compiler bin folder.
  exit /b 1
)

echo Using compiler: %MINGW%\g++.exe
set "PATH=%MINGW%;%PATH%"

set "BUILD_DIR=build"
set "HIDAPI_OBJ=%BUILD_DIR%\hidapi_control_panel.o"
set "GUI_DIR="
set "GUI_PARENT="

if exist "GUI\dlpc350_api.cpp" if exist "GUI\hidapi-master\windows\hid.c" (
  for %%D in ("GUI") do set "GUI_DIR=%%~fD"
  set "GUI_PARENT=%CD%"
)

if not defined GUI_DIR (
  echo [ERROR] LightCrafter 4500 GUI source folder was not found.
  echo Expected GUI\dlpc350_api.cpp and GUI\hidapi-master\windows\hid.c
  exit /b 1
)

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

echo [1/2] Compiling HIDAPI...
"%MINGW%\gcc.exe" -std=gnu11 -O2 -Wall -Wno-stringop-truncation ^
  -I"%GUI_DIR%\hidapi-master\hidapi" ^
  -c "%GUI_DIR%\hidapi-master\windows\hid.c" ^
  -o "%HIDAPI_OBJ%"

if errorlevel 1 (
  echo [ERROR] HIDAPI build failed.
  exit /b 1
)

echo [2/2] Building StructuredLightControlPanel.exe...
"%MINGW%\g++.exe" -std=c++17 -O2 -Wall -Wextra -municode -mwindows ^
  -I"%GUI_PARENT%" -I"%GUI_DIR%" -I"%GUI_DIR%\hidapi-master\hidapi" ^
  StructuredLightControlPanel.cpp ^
  dlpc350_usb_standalone.cpp ^
  "%GUI_DIR%\dlpc350_api.cpp" ^
  "%GUI_DIR%\dlpc350_common.cpp" ^
  "%HIDAPI_OBJ%" ^
  -o StructuredLightControlPanel.exe ^
  -lsetupapi -lhid -lcomctl32 -lshell32 -lole32 -luuid

if errorlevel 1 (
  echo [ERROR] Build failed.
  exit /b 1
)

echo Build complete: %CD%\StructuredLightControlPanel.exe
exit /b 0
