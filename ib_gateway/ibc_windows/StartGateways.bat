@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem 递归遍历 Documents\IBC 下的 config-*.ini，并为每个实例启动一个 IBC Gateway。
if /I "%~1"=="/?" goto :usage
if /I "%~1"=="/HELP" goto :usage
if /I "%~1"=="--instance" goto :run_instance
if /I "%~1"=="/DRYRUN" set "IBC_DRY_RUN=1"

rem 优先使用 IBC_PATH；否则使用脚本目录，或自动识别同级唯一的 IBCWin-* 目录。
set "IBC_SCRIPT_DIR=%~dp0"
if "%IBC_SCRIPT_DIR:~-1%"=="\" set "IBC_SCRIPT_DIR=%IBC_SCRIPT_DIR:~0,-1%"
if not defined IBC_PATH if exist "%IBC_SCRIPT_DIR%\scripts\DisplayBannerAndLaunch.bat" set "IBC_PATH=%IBC_SCRIPT_DIR%"
if not defined IBC_PATH (
  for %%D in ("%IBC_SCRIPT_DIR%\..") do set "IBC_SCRIPT_PARENT=%%~fD"
  set "IBC_CANDIDATE_COUNT=0"
  for /f "delims=" %%D in ('dir /b /ad "!IBC_SCRIPT_PARENT!\IBCWin-*" 2^>nul') do if exist "!IBC_SCRIPT_PARENT!\%%D\scripts\DisplayBannerAndLaunch.bat" (
    set /a IBC_CANDIDATE_COUNT+=1
    set "IBC_CANDIDATE_PATH=!IBC_SCRIPT_PARENT!\%%D"
  )
  if "!IBC_CANDIDATE_COUNT!"=="1" set "IBC_PATH=!IBC_CANDIDATE_PATH!"
  if !IBC_CANDIDATE_COUNT! GTR 1 (
    echo Multiple sibling IBCWin-* installations were found. Set IBC_PATH explicitly.
    exit /b 1005
  )
)
if not exist "%IBC_PATH%\scripts\DisplayBannerAndLaunch.bat" (
  echo IBC installation was not found. Put this script in the IBC root, keep one sibling IBCWin-* directory, or set IBC_PATH explicitly.
  exit /b 1005
)

rem 允许按安装目录、版本号、配置目录和日志目录覆盖默认值。
if not defined IBC_TWS_PATH if exist "C:\Jts" set "IBC_TWS_PATH=C:\Jts"
if not defined IBC_TWS_PATH if exist "D:\Jts" set "IBC_TWS_PATH=D:\Jts"
if not defined IBC_TWS_PATH (
  echo Gateway installation was not found. Set IBC_TWS_PATH to its root directory.
  exit /b 1004
)
if not defined IBC_TWS_MAJOR_VRSN if defined TWS_MAJOR_VRSN set "IBC_TWS_MAJOR_VRSN=%TWS_MAJOR_VRSN%"
if not defined IBC_TWS_MAJOR_VRSN (
  set "IBC_HIGHEST_VERSION=0"
  for /f "delims=" %%V in ('dir /b /ad "%IBC_TWS_PATH%\ibgateway" 2^>nul ^| findstr /r /x "[0-9][0-9]*"') do if exist "%IBC_TWS_PATH%\ibgateway\%%V\jars" (
    set /a IBC_VERSION_NUMBER=%%V >NUL 2>&1
    if !IBC_VERSION_NUMBER! GTR !IBC_HIGHEST_VERSION! (
      set "IBC_HIGHEST_VERSION=!IBC_VERSION_NUMBER!"
      set "IBC_TWS_MAJOR_VRSN=%%V"
    )
  )
  for /f "delims=" %%V in ('dir /b /ad "%IBC_TWS_PATH%" 2^>nul ^| findstr /r /x "[0-9][0-9]*"') do if exist "%IBC_TWS_PATH%\%%V\jars" (
    set /a IBC_VERSION_NUMBER=%%V >NUL 2>&1
    if !IBC_VERSION_NUMBER! GTR !IBC_HIGHEST_VERSION! (
      set "IBC_HIGHEST_VERSION=!IBC_VERSION_NUMBER!"
      set "IBC_TWS_MAJOR_VRSN=%%V"
    )
  )
)
if not defined IBC_TWS_MAJOR_VRSN (
  echo No numeric Gateway/TWS version with a jars directory was found under %IBC_TWS_PATH%.
  exit /b 1004
)
set "TWS_MAJOR_VRSN=%IBC_TWS_MAJOR_VRSN%"
if not exist "%IBC_TWS_PATH%\ibgateway\%TWS_MAJOR_VRSN%\jars" if not exist "%IBC_TWS_PATH%\%TWS_MAJOR_VRSN%\jars" (
  echo Gateway version %TWS_MAJOR_VRSN% was not found under %IBC_TWS_PATH%.
  exit /b 1004
)

if not defined IBC_CONFIG_DIR set "IBC_CONFIG_DIR=%USERPROFILE%\Documents\IBC"
if not exist "%IBC_CONFIG_DIR%" (
  echo Configuration directory was not found: %IBC_CONFIG_DIR%
  exit /b 1006
)
if not defined IBC_SETTINGS_ROOT set "IBC_SETTINGS_ROOT=%IBC_PATH%\settings"
if not defined IBC_LOG_ROOT set "IBC_LOG_ROOT=%IBC_PATH%\Logs"

set "IBC_FOUND=0"
set "IBC_VALID=0"
for /r "%IBC_CONFIG_DIR%" %%F in (config-*.ini) do (
  set "IBC_FOUND=1"
  set "IBC_CONFIG_FILE=%%~fF"
  set "IBC_INSTANCE=%%~nF"
  set "IBC_INSTANCE=!IBC_INSTANCE:config-=!"
  set "IBC_CONFIG_PARENT_NAME="
  for %%D in ("%%~dpF.") do set "IBC_CONFIG_PARENT_NAME=%%~nxD"
  set "IBC_CONFIG_VERSION=!IBC_CONFIG_PARENT_NAME!"
  for /f "delims=0123456789" %%X in ("!IBC_CONFIG_PARENT_NAME!") do set "IBC_CONFIG_VERSION="
  if defined IBC_CONFIG_VERSION (
    set "IBC_INSTANCE_KEY=!IBC_CONFIG_VERSION!-!IBC_INSTANCE!"
    set "IBC_SELECTED_VERSION=!IBC_CONFIG_VERSION!"
  ) else (
    set "IBC_INSTANCE_KEY=!IBC_INSTANCE!"
    set "IBC_SELECTED_VERSION=%TWS_MAJOR_VRSN%"
  )
  set "IBC_VERSION_DIR="
  if exist "%IBC_TWS_PATH%\ibgateway\!IBC_SELECTED_VERSION!\jars" set "IBC_VERSION_DIR=%IBC_TWS_PATH%\ibgateway\!IBC_SELECTED_VERSION!"
  if not defined IBC_VERSION_DIR if exist "%IBC_TWS_PATH%\!IBC_SELECTED_VERSION!\jars" set "IBC_VERSION_DIR=%IBC_TWS_PATH%\!IBC_SELECTED_VERSION!"
  if not defined IBC_INSTANCE (
    echo Skipping config-*.ini with an empty instance name: %%~fF
  ) else if not defined IBC_VERSION_DIR (
    echo Skipping !IBC_CONFIG_FILE!: Gateway version !IBC_SELECTED_VERSION! was not found.
  ) else if defined IBC_SEEN_!IBC_INSTANCE_KEY! (
    echo Skipping duplicate instance key !IBC_INSTANCE_KEY!: !IBC_CONFIG_FILE!
  ) else (
    set "IBC_VALID=1"
    set "IBC_SEEN_!IBC_INSTANCE_KEY!=1"
    set "IBC_INSTANCE_SETTINGS=%IBC_SETTINGS_ROOT%\!IBC_INSTANCE_KEY!"
    set "IBC_INSTANCE_LOG=%IBC_LOG_ROOT%\!IBC_INSTANCE_KEY!"
    if not exist "!IBC_INSTANCE_SETTINGS!" mkdir "!IBC_INSTANCE_SETTINGS!" >NUL 2>&1
    if not exist "!IBC_INSTANCE_SETTINGS!\jts.ini" if exist "!IBC_VERSION_DIR!\jts.ini" copy /Y "!IBC_VERSION_DIR!\jts.ini" "!IBC_INSTANCE_SETTINGS!\jts.ini" >NUL
    if not exist "!IBC_INSTANCE_SETTINGS!\jts.ini" if exist "%IBC_TWS_PATH%\jts.ini" copy /Y "%IBC_TWS_PATH%\jts.ini" "!IBC_INSTANCE_SETTINGS!\jts.ini" >NUL
    if not exist "!IBC_INSTANCE_LOG!" mkdir "!IBC_INSTANCE_LOG!" >NUL 2>&1
    if defined IBC_DRY_RUN (
      echo [DRYRUN] instance=!IBC_INSTANCE_KEY!
      echo [DRYRUN] version=!IBC_SELECTED_VERSION!
      echo [DRYRUN] config=!IBC_CONFIG_FILE!
      echo [DRYRUN] settings=!IBC_INSTANCE_SETTINGS!
      echo [DRYRUN] log=!IBC_INSTANCE_LOG!
    ) else (
      set "IBC_RUNNING_COUNT=0"
      for /f "delims=" %%P in ('powershell -NoLogo -NoProfile -Command "$p=[Regex]::Escape($env:IBC_CONFIG_FILE); @((Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) | Where-Object Name -EQ 'java.exe' | Where-Object CommandLine -Match $p).Count"') do set "IBC_RUNNING_COUNT=%%P"
      if not "!IBC_RUNNING_COUNT!"=="0" (
        echo Skipping running IBC Gateway instance !IBC_INSTANCE_KEY!: !IBC_CONFIG_FILE!
      ) else (
        echo Starting IBC Gateway instance !IBC_INSTANCE_KEY! from !IBC_CONFIG_FILE!
        start "IBC Gateway !IBC_INSTANCE_KEY!" /D "%IBC_PATH%" "%ComSpec%" /d /c call "%~f0" --instance "!IBC_INSTANCE_KEY!" "!IBC_CONFIG_FILE!" "!IBC_INSTANCE_SETTINGS!" "!IBC_INSTANCE_LOG!" "!IBC_SELECTED_VERSION!" "%IBC_TWS_PATH%"
      )
    )
  )
)

if "%IBC_FOUND%"=="0" (
  echo No configuration files found. Add config-^<instance^>.ini under %IBC_CONFIG_DIR%.
  exit /b 1006
)
if "%IBC_VALID%"=="0" (
  echo No valid Gateway configuration matched an installed version.
  exit /b 1004
)
exit /b 0

:run_instance
rem 子进程继承 IBC_PATH，仅替换本实例的安装版本、配置、设置和日志路径。
set "IBC_INSTANCE=%~2"
set "CONFIG=%~3"
set "TWS_SETTINGS_PATH=%~4"
set "LOG_PATH=%~5"
set "TWS_MAJOR_VRSN=%~6"
set "IBC_TWS_MAJOR_VRSN=%~6"
set "TWS_PATH=%~7"
set "IBC_TWS_PATH=%~7"
set "APP=GATEWAY"
set "INLINE=1"
set "TRADING_MODE="
if not defined IBC_TWOFA_TIMEOUT_ACTION set "IBC_TWOFA_TIMEOUT_ACTION=restart"
set "TWOFA_TIMEOUT_ACTION=%IBC_TWOFA_TIMEOUT_ACTION%"
call "%IBC_PATH%\scripts\DisplayBannerAndLaunch.bat" /INLINE
exit /b %ERRORLEVEL%

:usage
echo Usage: StartGateways.bat [/DRYRUN]
echo.
echo Recursively enumerates config-*.ini under IBC_CONFIG_DIR and starts one IBC Gateway per file.
echo A numeric parent directory pins a config to that Gateway version, for example:
echo   %%USERPROFILE%%\Documents\IBC\1045\config-live.ini
echo Configs directly under IBC_CONFIG_DIR use IBC_TWS_MAJOR_VRSN or the highest installed version.
echo Optional environment overrides:
echo   IBC_PATH          IBC installation root (default: script directory or one sibling IBCWin-* directory)
echo   IBC_TWS_PATH      TWS/Gateway installation root (default: C:\Jts or D:\Jts)
echo   IBC_TWS_MAJOR_VRSN Gateway major version for unpinned configs (default: auto-detect)
echo   IBC_CONFIG_DIR    config directory (default: %%USERPROFILE%%\Documents\IBC)
echo   IBC_SETTINGS_ROOT per-instance settings root
echo   IBC_LOG_ROOT      per-instance log root
echo   IBC_TWOFA_TIMEOUT_ACTION restart or exit after 2FA completion timeout (default: restart)
exit /b 0
