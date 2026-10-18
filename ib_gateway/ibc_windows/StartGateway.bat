@echo off
setlocal EnableExtensions

rem Empty shortcut Start-in leaves cwd at the shortcut directory. Enter the script directory first.
set "IBC_SCRIPT_DIR=%~dp0"
if "%IBC_SCRIPT_DIR:~-1%"=="\" set "IBC_SCRIPT_DIR=%IBC_SCRIPT_DIR:~0,-1%"
cd /d "%IBC_SCRIPT_DIR%"
if errorlevel 1 (
  echo Cannot enter script directory: %IBC_SCRIPT_DIR%
  exit /b 1005
)
if not exist "%IBC_SCRIPT_DIR%\StartGateways.bat" (
  echo StartGateways.bat was not found beside StartGateway.bat.
  exit /b 1005
)
call "%IBC_SCRIPT_DIR%\StartGateways.bat" %*
set "IBC_EXIT=%ERRORLEVEL%"
exit /b %IBC_EXIT%
