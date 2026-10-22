param(
    [Parameter(Mandatory = $true)][int]$ParentPid,
    [Parameter(Mandatory = $true)][int]$ApiPort,
    [Parameter(Mandatory = $true)][string]$ConfigFile,
    [Parameter(Mandatory = $true)][string]$LogFile,
    [string]$SettingsPath = '',
    [int]$RestartHangSeconds = 180
)

$ErrorActionPreference = 'SilentlyContinue'
$escapedConfig = [regex]::Escape($ConfigFile)

function Write-WatchLog([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} WatchGateway: {1}' -f (Get-Date), $Message
    if ($LogFile) {
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    }
}

function Test-ParentAlive {
    return [bool](Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)
}

function Get-TargetJava {
    return @(Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $escapedConfig) })
}

function Test-ApiListening {
    try {
        $conns = @(Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction Stop)
        if ($conns.Count -gt 0) { return $true }
    } catch { }
    $pattern = ':' + $ApiPort + '\s+.*LISTENING'
    return [bool](netstat -ano | Select-String -Pattern $pattern)
}

function Get-LogTail {
    if (-not (Test-Path -LiteralPath $LogFile)) { return '' }
    return ((Get-Content -LiteralPath $LogFile -Tail 120 -ErrorAction SilentlyContinue) -join "`n")
}

function Test-AutoRestartToken {
    if (-not $SettingsPath) { return $false }
    if (-not (Test-Path -LiteralPath $SettingsPath)) { return $false }
    return [bool](Get-ChildItem -LiteralPath $SettingsPath -Filter 'autorestart' -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)
}

Write-WatchLog "watching API port $ApiPort parent=$ParentPid (silent restart only)"
$notReadySince = $null

while (Test-ParentAlive) {
    Start-Sleep -Seconds 15
    $javas = Get-TargetJava
    if ($javas.Count -eq 0) {
        $notReadySince = $null
        continue
    }
    if (Test-ApiListening) {
        $notReadySince = $null
        continue
    }

    $tail = Get-LogTail
    $has2FA = $tail -match 'Second Factor Authentication|Passkey Authentication'
    $isSilentRestart = ($tail -match 'Re-starting session') -or (Test-AutoRestartToken)
    if ($has2FA -or -not $isSilentRestart) {
        $notReadySince = $null
        continue
    }

    if ($null -eq $notReadySince) {
        $notReadySince = Get-Date
        Write-WatchLog "silent restart hung on port $ApiPort; starting timer ($RestartHangSeconds s)"
    }

    $waited = [int]((Get-Date) - $notReadySince).TotalSeconds
    if ($waited -ge $RestartHangSeconds) {
        Write-WatchLog "API port $ApiPort still down after ${waited}s during silent restart; killing hung java"
        foreach ($proc in $javas) {
            try {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                Write-WatchLog "killed pid $($proc.ProcessId)"
            } catch {
                Write-WatchLog "kill failed pid $($proc.ProcessId): $_"
            }
        }
        $notReadySince = $null
    }
}

Write-WatchLog 'parent process exited; watchdog stopping'
