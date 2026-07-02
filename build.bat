@echo off
setlocal
cd /d "%~dp0"

set "BUILD_DIR=build"
set "HIDAPI_OBJ=%BUILD_DIR%\hidapi.o"

if defined MINGW (
    if not exist "%MINGW%\g++.exe" (
        echo [WARN] MINGW is set, but g++.exe was not found:
        echo        %MINGW%\g++.exe
        set "MINGW="
    )
)

if not defined MINGW (
    for %%D in ("%MSYSTEM_PREFIX%\bin" "C:\msys64\ucrt64\bin" "C:\msys64\mingw64\bin" "C:\msys64\clang64\bin") do (
        if not defined MINGW if exist "%%~D\g++.exe" set "MINGW=%%~D"
    )
)

if not defined MINGW (
    for %%G in (g++.exe) do set "GXX=%%~$PATH:G"
    if defined GXX for %%D in ("%GXX%") do set "MINGW=%%~dpD"
)

if not defined MINGW (
    echo [ERROR] MinGW-w64 g++.exe was not found:
    echo.
    echo Checked:
    echo   %%MINGW%% environment variable
    echo   %%MSYSTEM_PREFIX%%\bin
    echo   C:\msys64\ucrt64\bin
    echo   C:\msys64\mingw64\bin
    echo   C:\msys64\clang64\bin
    echo   PATH
    echo.
    echo Install MSYS2 MinGW-w64, or run this after setting MINGW to the compiler bin folder:
    echo   set MINGW=C:\msys64\ucrt64\bin
    exit /b 1
)

echo Using compiler: %MINGW%\g++.exe
set "PATH=%MINGW%;%PATH%"

set "GUI_DIR="
set "GUI_PARENT="

if exist "GUI\dlpc350_api.cpp" if exist "GUI\hidapi-master\windows\hid.c" (
    for %%D in ("GUI") do set "GUI_DIR=%%~fD"
    set "GUI_PARENT=%CD%"
)

if not defined GUI_DIR (
    for /d /r "%CD%" %%D in (GUI) do (
        if not defined GUI_DIR if exist "%%D\dlpc350_api.cpp" if exist "%%D\hidapi-master\windows\hid.c" (
            set "GUI_DIR=%%D"
            for %%P in ("%%D\..") do set "GUI_PARENT=%%~fP"
        )
    )
)

if not defined GUI_DIR (
    echo [ERROR] LightCrafter 4500 GUI source folder was not found.
    echo.
    echo Expected a GUI folder somewhere under:
    echo   %CD%
    echo.
    echo Extract LightCrafter4500_GUI_Source_Code_v3.1.0 into this project folder
    echo so that dlpc350_api.cpp and hidapi-master\windows\hid.c are under a GUI folder.
    exit /b 1
)

echo Using GUI source: %GUI_DIR%

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

echo [1/2] Compiling HIDAPI...
"%MINGW%\gcc.exe" -std=gnu11 -O2 -Wall -Wno-stringop-truncation ^
    -I"%GUI_DIR%\hidapi-master\hidapi" ^
    -c "%GUI_DIR%\hidapi-master\windows\hid.c" ^
    -o "%HIDAPI_OBJ%"
if errorlevel 1 goto :fail
if not exist "%HIDAPI_OBJ%" (
    echo [ERROR] HIDAPI object file was not created:
    echo         %CD%\%HIDAPI_OBJ%
    goto :fail
)

echo [2/2] Building PRO4500.exe...
"%MINGW%\g++.exe" -std=c++17 -O2 -Wall -Wextra -municode -mwindows ^
    -I"%GUI_PARENT%" -I"%GUI_DIR%" -I"%GUI_DIR%\hidapi-master\hidapi" ^
    "PRO4500.cpp" ^
    "dlpc350_usb_standalone.cpp" ^
    "%GUI_DIR%\dlpc350_api.cpp" ^
    "%GUI_DIR%\dlpc350_common.cpp" ^
    "%HIDAPI_OBJ%" ^
    -o "PRO4500.exe" ^
    -lsetupapi -lhid -lgdiplus -lcomctl32 -lole32 -luuid
if errorlevel 1 goto :fail

echo Copying MinGW runtime DLLs...
copy /y "%MINGW%\libgcc_s_seh-1.dll" . >nul
copy /y "%MINGW%\libstdc++-6.dll" . >nul
copy /y "%MINGW%\libwinpthread-1.dll" . >nul

echo.
echo Build complete: %CD%\PRO4500.exe
exit /b 0

:fail
echo.
echo [ERROR] Build failed.
exit /b 1
