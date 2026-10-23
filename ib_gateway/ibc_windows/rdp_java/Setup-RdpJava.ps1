#Requires -Version 5.1
param(
    [switch]$SkipDownload
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$jreHome = Join-Path $here 'jre-21-fx'
$javaExe = Join-Path $jreHome 'bin\java.exe'
$legacyJre = 'C:\Jts\jre-21-fx'
$libericaUrl = 'https://github.com/bell-sw/Liberica/releases/download/21.0.13+11/bellsoft-jre21.0.13+11-windows-amd64-full.zip'

function Test-JavaFxJre([string]$homeDir) {
    $exe = Join-Path $homeDir 'bin\java.exe'
    if (-not (Test-Path -LiteralPath $exe)) { return $false }
    $fx = & $exe --list-modules 2>$null | Select-String -Pattern '^javafx\.graphics@'
    return [bool]$fx
}

function Install-JavaFxJre {
    if (Test-JavaFxJre $jreHome) {
        Write-Host "JavaFX JRE already present: $jreHome"
        return
    }
    if ((Test-JavaFxJre $legacyJre) -and -not (Test-Path -LiteralPath $jreHome)) {
        Write-Host "Linking $jreHome -> $legacyJre"
        try {
            New-Item -ItemType Junction -Path $jreHome -Target $legacyJre | Out-Null
            return
        } catch {
            Write-Host "Junction failed, copying JRE into rdp_java."
            Copy-Item -LiteralPath $legacyJre -Destination $jreHome -Recurse -Force
            return
        }
    }
    if ($SkipDownload) {
        throw "JavaFX JRE not found at $jreHome. Run rdp_java\Setup-RdpJava.ps1 without -SkipDownload."
    }

    $zip = Join-Path $env:TEMP 'liberica21-full.zip'
    $extract = Join-Path $env:TEMP 'liberica21-full'
    Write-Host "Downloading Liberica 21 Full (JavaFX)..."
    Invoke-WebRequest -Uri $libericaUrl -OutFile $zip -UseBasicParsing
    if (Test-Path -LiteralPath $extract) {
        Remove-Item -LiteralPath $extract -Recurse -Force
    }
    Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
    $root = Get-ChildItem -LiteralPath $extract -Directory | Select-Object -First 1
    if (-not $root) { throw 'Liberica zip did not contain a JRE directory.' }
    if (Test-Path -LiteralPath $jreHome) {
        Remove-Item -LiteralPath $jreHome -Recurse -Force
    }
    Move-Item -LiteralPath $root.FullName -Destination $jreHome
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    if (-not (Test-JavaFxJre $jreHome)) {
        throw "Installed JRE at $jreHome is missing JavaFX."
    }
    Write-Host "Installed JavaFX JRE: $jreHome"
}

function Apply-RdpSessionKeepalive {
    $policy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services'
    if (-not (Test-Path $policy)) {
        New-Item -Path $policy -Force | Out-Null
    }
    $policyVals = @{
        MaxDisconnectionTime  = 0
        MaxIdleTime           = 0
        MaxConnectionTime     = 0
        fResetBroken          = 0
        fSingleSessionPerUser = 1
    }
    foreach ($name in $policyVals.Keys) {
        New-ItemProperty -Path $policy -Name $name -PropertyType DWord -Value $policyVals[$name] -Force | Out-Null
    }

    $ts = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
    New-ItemProperty -Path $ts -Name 'fSingleSessionPerUser' -PropertyType DWord -Value 1 -Force | Out-Null
    New-ItemProperty -Path $ts -Name 'KeepAliveEnable' -PropertyType DWord -Value 1 -Force | Out-Null
    New-ItemProperty -Path $ts -Name 'KeepAliveInterval' -PropertyType DWord -Value 1 -Force | Out-Null

    $rdp = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
    $rdpVals = @{
        fResetBroken                 = 0
        fInheritResetBroken          = 0
        MaxDisconnectionTime         = 0
        MaxIdleTime                  = 0
        MaxConnectionTime            = 0
        fInheritMaxDisconnectionTime = 0
        fInheritMaxIdleTime          = 0
        fInheritMaxSessionTime       = 0
    }
    foreach ($name in $rdpVals.Keys) {
        New-ItemProperty -Path $rdp -Name $name -PropertyType DWord -Value $rdpVals[$name] -Force | Out-Null
    }
    Write-Host 'RDP session-keep settings applied.'
}

Install-JavaFxJre
try {
    Apply-RdpSessionKeepalive
} catch {
    Write-Warning "Could not apply RDP session-keep settings. Run as Administrator. $_"
}

Write-Output $javaExe
