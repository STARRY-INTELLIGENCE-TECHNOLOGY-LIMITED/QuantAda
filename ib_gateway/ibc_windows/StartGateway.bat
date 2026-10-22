@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Recursively start one IBC Gateway per Documents\IBC\config-*.ini.
if /I "%~1"=="/?" goto :usage
if /I "%~1"=="/HELP" goto :usage
if /I "%~1"=="--instance" goto :run_instance
if /I "%~1"=="/DRYRUN" set "IBC_DRY_RUN=1"

rem Prefer IBC_PATH, else the script directory, else a sibling IBCWin-* directory.
rem Empty shortcut Start-in leaves cwd on the desktop. Enter the script directory first.
set "IBC_SCRIPT_DIR=%~dp0"
if "%IBC_SCRIPT_DIR:~-1%"=="\" set "IBC_SCRIPT_DIR=%IBC_SCRIPT_DIR:~0,-1%"
cd /d "%IBC_SCRIPT_DIR%"
if errorlevel 1 (
  echo Cannot enter script directory: %IBC_SCRIPT_DIR%
  exit /b 1005
)
if not defined IBC_PATH if exist "%IBC_SCRIPT_DIR%\scripts\DisplayBannerAndLaunch.bat" set "IBC_PATH=%IBC_SCRIPT_DIR%"
if not defined IBC_PATH (
  for %%D in ("%IBC_SCRIPT_DIR%\..") do set "IBC_SCRIPT_PARENT=%%~fD"
  rem Prefer the real IBCWin-3.24.2 tree. Ignore date-mangled IBCWin-3.20* leftovers.
  if exist "!IBC_SCRIPT_PARENT!\IBCWin-3.24.2\IBC.jar" if exist "!IBC_SCRIPT_PARENT!\IBCWin-3.24.2\scripts\DisplayBannerAndLaunch.bat" set "IBC_PATH=!IBC_SCRIPT_PARENT!\IBCWin-3.24.2"
)
if not defined IBC_PATH (
  set "IBC_CANDIDATE_COUNT=0"
  for /f "delims=" %%D in ('dir /b /ad "!IBC_SCRIPT_PARENT!\IBCWin-*" 2^>nul') do (
    echo %%D| findstr /r /c:"IBCWin-3.20[0-9][0-9][0-9][0-9][0-9][0-9]\." >NUL
    if errorlevel 1 if exist "!IBC_SCRIPT_PARENT!\%%D\scripts\DisplayBannerAndLaunch.bat" if exist "!IBC_SCRIPT_PARENT!\%%D\IBC.jar" (
      set /a IBC_CANDIDATE_COUNT+=1
      set "IBC_CANDIDATE_PATH=!IBC_SCRIPT_PARENT!\%%D"
    )
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
cd /d "%IBC_PATH%"
if errorlevel 1 (
  echo Cannot enter IBC installation: %IBC_PATH%
  exit /b 1005
)
set "IBC_PATH=%CD%"

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

if not defined IBC_DRY_RUN call :apply_rdp_keepalive

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
        start "IBC Gateway !IBC_INSTANCE_KEY!" /D "!IBC_PATH!" "%ComSpec%" /d /c call "%~f0" --instance "!IBC_INSTANCE_KEY!" "!IBC_CONFIG_FILE!" "!IBC_INSTANCE_SETTINGS!" "!IBC_INSTANCE_LOG!" "!IBC_SELECTED_VERSION!" "%IBC_TWS_PATH%"
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
if not defined IBC_PATH (
  echo IBC_PATH is not set.
  exit /b 1005
)
if "!IBC_PATH:~-1!"=="\" set "IBC_PATH=!IBC_PATH:~0,-1!"
cd /d "!IBC_PATH!"
if errorlevel 1 (
  echo Cannot enter IBC installation: !IBC_PATH!
  exit /b 1005
)
set "IBC_PATH=!CD!"
set "IBC_INSTANCE=%~2"
set "CONFIG=%~3"
set "TWS_SETTINGS_PATH=%~4"
set "LOG_PATH=%~5"
set "TWS_MAJOR_VRSN=%~6"
set "IBC_TWS_MAJOR_VRSN=%~6"
set "TWS_PATH=%~7"
set "IBC_TWS_PATH=%~7"
set "APP=GATEWAY"
set "TRADING_MODE="
if not defined IBC_TWOFA_TIMEOUT_ACTION set "IBC_TWOFA_TIMEOUT_ACTION=restart"
set "TWOFA_TIMEOUT_ACTION=%IBC_TWOFA_TIMEOUT_ACTION%"
if defined TWS_SETTINGS_PATH if "%TWS_SETTINGS_PATH:~-1%"=="\" set "TWS_SETTINGS_PATH=%TWS_SETTINGS_PATH:~0,-1%"
set "IBC_SETTINGS_ABS="
if "%TWS_SETTINGS_PATH:~1,2%"==":\" set "IBC_SETTINGS_ABS=1"
if "%TWS_SETTINGS_PATH:~0,2%"=="\\" set "IBC_SETTINGS_ABS=1"
if not defined IBC_SETTINGS_ABS (
  echo Settings path must be absolute: %TWS_SETTINGS_PATH%
  exit /b 1008
)
if not exist "%TWS_SETTINGS_PATH%" (
  echo Settings directory was not found: %TWS_SETTINGS_PATH%
  exit /b 1008
)
cd /d "%TWS_SETTINGS_PATH%"
if errorlevel 1 (
  echo Cannot enter settings directory: %TWS_SETTINGS_PATH%
  exit /b 1008
)
set "TWS_SETTINGS_PATH=%CD%"
for %%D in ("%USERPROFILE%\Desktop") do if /I "%TWS_SETTINGS_PATH%"=="%%~fD" (
  echo Refusing to use the Desktop as the Gateway settings directory.
  exit /b 1008
)
if not exist "%IBC_PATH%\scripts\StartIBC.bat" (
  echo StartIBC.bat was not found: %IBC_PATH%\scripts\StartIBC.bat
  exit /b 1005
)
set "IBC_VRSN="
if exist "%IBC_PATH%\version" for /f "usebackq delims=" %%i in ("%IBC_PATH%\version") do if not defined IBC_VRSN set "IBC_VRSN=%%i"
if not defined IBC_VRSN set "IBC_VRSN=unknown"
set "LOG_FILE=NUL"
if not defined LOG_PATH goto :log_ready
if /I "%LOG_PATH%"=="CON" (
  set "LOG_FILE=CON"
  goto :log_ready
)
set "IBC_LOG_ABS="
if "%LOG_PATH:~1,2%"==":\" set "IBC_LOG_ABS=1"
if "%LOG_PATH:~0,2%"=="\\" set "IBC_LOG_ABS=1"
if not defined IBC_LOG_ABS (
  echo LOG_PATH must be an absolute directory: %LOG_PATH%
  exit /b 1
)
if not exist "%LOG_PATH%" mkdir "%LOG_PATH%"
if not exist "%LOG_PATH%" (
  echo Cannot create log directory: %LOG_PATH%
  exit /b 1
)
set "DAYOFWEEK=UNKNOWN"
for /f "delims=" %%a in ('powershell -NoLogo -NoProfile -Command "(Get-Date).DayOfWeek.ToString().ToUpper()"') do set "DAYOFWEEK=%%a"
set "LOG_FILE=%LOG_PATH%\IBC-%IBC_VRSN%_%APP%-%TWS_MAJOR_VRSN%_%DAYOFWEEK%.txt"
if exist "%LOG_FILE%" powershell -NoLogo -NoProfile -Command "if ((Get-Item -LiteralPath $env:LOG_FILE).LastWriteTime.Date -lt (Get-Date).Date) { Remove-Item -LiteralPath $env:LOG_FILE -Force }"
:log_ready
set "IBC_STAMP="
for /f "delims=" %%t in ('powershell -NoLogo -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "IBC_STAMP=%%t"
echo +==============================================================================
echo +
echo + IBC version %IBC_VRSN%
echo +
echo + Running %APP% %TWS_MAJOR_VRSN% at %IBC_STAMP%
echo +
if /I not "%LOG_FILE%"=="NUL" if /I not "%LOG_FILE%"=="CON" (
  echo + Diagnostic information is logged in:
  echo +
  echo + %LOG_FILE%
  echo +
  >>"%LOG_FILE%" echo Diagnostic log: %LOG_FILE%
)
echo +
echo + ** Caution: closing this window will close %APP% %TWS_MAJOR_VRSN% **
echo +
set "IBC_WATCH=%~dp0WatchGateway.ps1"
set "IBC_WATCH_PORT="
for /f "tokens=2 delims==" %%a in ('findstr /i /b /c:"OverrideTwsApiPort=" "%CONFIG%" 2^>nul') do set "IBC_WATCH_PORT=%%a"
if not defined IBC_WATCH_PORT (
  echo %IBC_INSTANCE%| findstr /r /x "[0-9][0-9]*" >NUL
  if not errorlevel 1 set "IBC_WATCH_PORT=%IBC_INSTANCE%"
)
set "IBC_WATCH_PARENT="
for /f "delims=" %%p in ('powershell -NoLogo -NoProfile -Command "(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId"') do set "IBC_WATCH_PARENT=%%p"
if exist "%IBC_WATCH%" if defined IBC_WATCH_PORT if defined IBC_WATCH_PARENT (
  echo Starting API watchdog on port %IBC_WATCH_PORT%
  >>"%LOG_FILE%" echo WatchGateway: starting on port %IBC_WATCH_PORT% parent=%IBC_WATCH_PARENT%
  start "" /b powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%IBC_WATCH%" -ParentPid %IBC_WATCH_PARENT% -ApiPort %IBC_WATCH_PORT% -ConfigFile "%CONFIG%" -LogFile "%LOG_FILE%" -SettingsPath "%TWS_SETTINGS_PATH%" >NUL 2>&1
)
:ibc_keep_alive
call "%IBC_PATH%\scripts\StartIBC.bat" "%TWS_MAJOR_VRSN%" /G "/TwsPath:%TWS_PATH%" "/TwsSettingsPath:%TWS_SETTINGS_PATH%" "/IbcPath:%IBC_PATH%" "/Config:%CONFIG%" "/User:%TWSUSERID%" "/PW:%TWSPASSWORD%" "/FIXUser:%FIXUSERID%" "/FIXPW:%FIXPASSWORD%" "/JavaPath:%JAVA_PATH%" "/Mode:%TRADING_MODE%" "/On2FATimeout:%TWOFA_TIMEOUT_ACTION%" >> "%LOG_FILE%" 2>&1
set "IBC_EXIT=%ERRORLEVEL%"
if exist "%TWS_SETTINGS_PATH%\STOP" (
  echo Stop file found, not relaunching.
  exit /b %IBC_EXIT%
)
rem Only relaunch when IB left an autorestart token. A cold start would wait for passkey.
set "IBC_HAS_AUTORESTART="
for /f "delims=" %%A in ('where /R "%TWS_SETTINGS_PATH%" autorestart 2^>nul') do set "IBC_HAS_AUTORESTART=1"
if defined IBC_HAS_AUTORESTART (
  echo Silent autorestart token found. Relaunching without passkey.
  >>"%LOG_FILE%" echo Silent autorestart token found. Relaunching without passkey.
  ping localhost -n 16 >NUL
  goto :ibc_keep_alive
)
echo No autorestart token. Not starting a passkey login.
>>"%LOG_FILE%" echo No autorestart token. Not starting a passkey login.
echo Gateway stopped. This window stays open so the instance is visible.
pause
exit /b %IBC_EXIT%
:usage
echo Usage: StartGateway.bat [/DRYRUN]
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
echo Shortcut Start-in may be empty. This script does not use the caller working directory.
echo Vendor DisplayBannerAndLaunch.bat is not used. Its DATE parsing can create desktop folders.
echo Daily AutoRestart reuses the weekly session. The watchdog only kills a hung silent restart.
echo The launcher does not start a new passkey login by itself.
echo On start it keeps disconnected RDP sessions and reconnects the same user to the existing session.
exit /b 0
:apply_rdp_keepalive
rem Closing the RDP window must disconnect, not log off. Reconnect must resume this session.
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v MaxDisconnectionTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v MaxIdleTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v MaxConnectionTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v fResetBroken /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v fSingleSessionPerUser /t REG_DWORD /d 1 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v fSingleSessionPerUser /t REG_DWORD /d 1 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v KeepAliveEnable /t REG_DWORD /d 1 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v KeepAliveInterval /t REG_DWORD /d 1 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v fResetBroken /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v fInheritResetBroken /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v MaxDisconnectionTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v MaxIdleTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v MaxConnectionTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v fInheritMaxDisconnectionTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v fInheritMaxIdleTime /t REG_DWORD /d 0 /f >NUL 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" /v fInheritMaxSessionTime /t REG_DWORD /d 0 /f >NUL 2>&1
if errorlevel 1 echo Warning: could not apply RDP session-keep settings. Run StartGateway as Administrator.
goto :eof
