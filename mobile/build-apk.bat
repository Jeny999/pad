@echo off
setlocal
cd /d "%~dp0"
if exist "..\..\android-env.bat" call "..\..\android-env.bat"
set PATH=%USERPROFILE%\.cargo\bin;%PATH%
echo Building Pad debug APK (arm64)...
cargo tauri android build --debug --apk --target aarch64 --ci
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo APK files:
for /r "src-tauri\gen\android\app\build\outputs\apk" %%f in (*.apk) do echo   %%f
endlocal
