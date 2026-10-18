@echo off
setlocal EnableExtensions

rem 在桌面重建 StartGateway 快捷方式，并把起始位置固定为脚本目录。目标必须是已解压的 bat，不能指向 zip 内部。
set "IBC_SCRIPT_DIR=%~dp0"
if "%IBC_SCRIPT_DIR:~-1%"=="\" set "IBC_SCRIPT_DIR=%IBC_SCRIPT_DIR:~0,-1%"
cd /d "%IBC_SCRIPT_DIR%"
if errorlevel 1 (
  echo Cannot enter script directory: %IBC_SCRIPT_DIR%
  exit /b 1005
)
set "IBC_LAUNCHER=%IBC_SCRIPT_DIR%\StartGateway.bat"
if not exist "%IBC_LAUNCHER%" set "IBC_LAUNCHER=%IBC_SCRIPT_DIR%\StartGateways.bat"
if not exist "%IBC_LAUNCHER%" (
  echo StartGateway.bat was not found.
  exit /b 1005
)
if not exist "%USERPROFILE%\Desktop" (
  echo Desktop directory was not found: %USERPROFILE%\Desktop
  exit /b 1005
)
powershell -NoLogo -NoProfile -Command "$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $env:USERPROFILE 'Desktop\StartGateway.lnk')); $shortcut.TargetPath=$env:IBC_LAUNCHER; $shortcut.WorkingDirectory=$env:IBC_SCRIPT_DIR; $shortcut.Save()"
if errorlevel 1 (
  echo Cannot create desktop shortcut.
  exit /b 1005
)
echo Created %USERPROFILE%\Desktop\StartGateway.lnk
echo Working directory: %IBC_SCRIPT_DIR%
exit /b 0
