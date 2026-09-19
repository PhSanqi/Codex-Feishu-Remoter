param(
    [string]$ExePath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'desktop_dist\CFR.exe'),
    [ValidateRange(1, 50)]
    [int]$ColdStarts = 5
)

$ErrorActionPreference = 'Stop'
$ExePath = (Resolve-Path $ExePath).Path
$RealLocalAppData = $env:LOCALAPPDATA
$TempRoot = $null
$RootProcess = $null
$SecondProcess = $null
$OwnedIds = @()

Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class CfrDesktopSmokeUser32 {
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
'@

function Get-DescendantIds([int]$RootPid) {
    $rows = @(Get-CimInstance Win32_Process)
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'
    $queue = New-Object 'System.Collections.Generic.Queue[int]'
    [void]$ids.Add($RootPid)
    $queue.Enqueue($RootPid)
    while ($queue.Count) {
        $parent = $queue.Dequeue()
        foreach ($row in $rows | Where-Object { [int]$_.ParentProcessId -eq $parent }) {
            $child = [int]$row.ProcessId
            if ($ids.Add($child)) { $queue.Enqueue($child) }
        }
    }
    return @($ids)
}

function Start-Cfr([string]$DataRoot) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $ExePath
    $psi.WorkingDirectory = Split-Path -Parent $ExePath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['LOCALAPPDATA'] = $DataRoot
    $psi.EnvironmentVariables['APPDATA'] = Join-Path $DataRoot 'Roaming'
    return [System.Diagnostics.Process]::Start($psi)
}

function Get-EndpointPorts([int[]]$Ids) {
    $control = @()
    $cdp = @()
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
        $_.LocalAddress -eq '127.0.0.1' -and $Ids -contains [int]$_.OwningProcess
    })
    foreach ($listener in $listeners) {
        $port = [int]$listener.LocalPort
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/api/v1/status" -TimeoutSec 1 | Out-Null
        } catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403) {
                $control += $port
            }
        }
        try {
            $version = Invoke-RestMethod -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 1
            if ($version.webSocketDebuggerUrl) { $cdp += $port }
        } catch {}
    }
    return [pscustomobject]@{
        Control = @($control | Sort-Object -Unique)
        Cdp = @($cdp | Sort-Object -Unique)
    }
}

function Stop-Owned([int[]]$Ids) {
    foreach ($processId in ($Ids | Sort-Object -Descending)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Wait-CfrReady([System.Diagnostics.Process]$Root, [int]$TimeoutSeconds = 45) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Root.HasExited) { throw "CFR exited before ready: $($Root.ExitCode)" }
        $ids = Get-DescendantIds $Root.Id
        $window = Get-Process -Id $ids -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -eq 'CFR' -and $_.MainWindowTitle -eq 'CFR' -and $_.MainWindowHandle -ne 0 } |
            Select-Object -First 1
        $ports = Get-EndpointPorts $ids
        if ($window -and $ports.Control.Count -eq 1 -and $ports.Cdp.Count -eq 1) {
            return [pscustomobject]@{ Window = $window; Ports = $ports; Ids = @($ids) }
        }
        Start-Sleep -Milliseconds 200
    }
    throw 'CFR main window / Control API / WebView2 CDP did not become ready before timeout'
}

function Get-CodexHostShimIds {
    return @(Get-CimInstance Win32_Process -Filter "Name = 'codexhost-shim.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.ProcessId } | Sort-Object)
}

function Get-LegacyChatMarkerState {
    $marker = Join-Path $RealLocalAppData 'CFR\browser\chatgpt-authenticated'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { return 'missing' }
    return 'present:' + (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash
}

function Stop-IsolatedRun([System.Diagnostics.Process]$Root, [int[]]$Ids, [string]$DataRoot) {
    Stop-Owned $Ids
    try { $Root.WaitForExit(5000) | Out-Null } catch {}
    $escaped = [regex]::Escape($DataRoot)
    $cleanupDeadline = (Get-Date).AddSeconds(5)
    do {
        $remaining = @(Get-Process -Id $Ids -ErrorAction SilentlyContinue)
        $dataRootProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and $_.CommandLine -match $escaped
        })
        if (-not $remaining.Count -and -not $dataRootProcesses.Count) { return }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $cleanupDeadline)
    if ($remaining.Count -or $dataRootProcesses.Count) {
        $remainingSummary = @($remaining | ForEach-Object { "$($_.Id):$($_.ProcessName)" }) -join ','
        $dataRootSummary = @($dataRootProcesses | ForEach-Object { "$($_.ProcessId):$($_.Name)" }) -join ','
        foreach ($process in $dataRootProcesses) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        throw "isolated CFR left owned process(es): known=$($remaining.Count) [$remainingSummary], data-root=$($dataRootProcesses.Count) [$dataRootSummary]"
    }
}

$BaseRoot = Join-Path $env:TEMP ('cfr-desktop-smoke-' + [guid]::NewGuid().ToString('N'))
$CodexHostBefore = Get-CodexHostShimIds
$LegacyMarkerBefore = Get-LegacyChatMarkerState
$ColdResults = @()
New-Item -ItemType Directory -Force -Path $BaseRoot | Out-Null
try {
    for ($run = 1; $run -le $ColdStarts; $run++) {
        $TempRoot = Join-Path $BaseRoot "cold-$run"
        New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
        $RootProcess = Start-Cfr $TempRoot
        try {
            $ready = Wait-CfrReady $RootProcess
            $OwnedIds = @($ready.Ids)
            $visibleTerminals = @(Get-Process -Id $OwnedIds -ErrorAction SilentlyContinue | Where-Object {
                $_.ProcessName -in @('cmd', 'powershell', 'pwsh', 'conhost') -and $_.MainWindowHandle -ne 0
            })
            if ($visibleTerminals.Count) { throw "CFR exposed $($visibleTerminals.Count) visible terminal window(s)" }
            $ColdResults += [ordered]@{
                run = $run
                control_port = $ready.Ports.Control[0]
                cdp_port = $ready.Ports.Cdp[0]
                visible_terminals = 0
            }
        } finally {
            if ($RootProcess -and -not $RootProcess.HasExited) {
                try { $OwnedIds = Get-DescendantIds $RootProcess.Id } catch { $OwnedIds = @($RootProcess.Id) }
                Stop-IsolatedRun $RootProcess $OwnedIds $TempRoot
            }
            $RootProcess = $null
            Remove-Item $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $TempRoot = Join-Path $BaseRoot 'tray-single-instance'
    New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
    $RootProcess = Start-Cfr $TempRoot
    $ready = Wait-CfrReady $RootProcess
    $Window = $ready.Window
    $Ports = $ready.Ports
    $OwnedIds = @($ready.Ids)
    $visibleTerminals = @(Get-Process -Id $OwnedIds -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -in @('cmd', 'powershell', 'pwsh', 'conhost') -and $_.MainWindowHandle -ne 0
    })
    if ($visibleTerminals.Count) { throw "CFR exposed $($visibleTerminals.Count) visible terminal window(s)" }

    $handle = [IntPtr]$Window.MainWindowHandle
    if (-not [CfrDesktopSmokeUser32]::IsWindowVisible($handle)) { throw 'CFR main window is unexpectedly hidden' }
    if (-not $Window.CloseMainWindow()) { throw 'could not request CFR close-to-tray' }
    $hideDeadline = (Get-Date).AddSeconds(3)
    while ([CfrDesktopSmokeUser32]::IsWindowVisible($handle) -and (Get-Date) -lt $hideDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if ($RootProcess.HasExited) { throw 'closing the CFR window exited the tray runtime' }
    if ([CfrDesktopSmokeUser32]::IsWindowVisible($handle)) { throw 'CFR window did not hide to tray' }

    $SecondProcess = Start-Cfr $TempRoot
    if (-not $SecondProcess.WaitForExit(20000)) { throw 'second CFR launch did not exit under the single-instance guard' }
    if ($SecondProcess.ExitCode -ne 0) { throw "second CFR launch returned $($SecondProcess.ExitCode)" }
    $showDeadline = (Get-Date).AddSeconds(3)
    while (-not [CfrDesktopSmokeUser32]::IsWindowVisible($handle) -and (Get-Date) -lt $showDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not [CfrDesktopSmokeUser32]::IsWindowVisible($handle)) { throw 'second CFR launch did not restore the tray-hidden window' }

    $OwnedIds = Get-DescendantIds $RootProcess.Id
    $PortsAfter = Get-EndpointPorts $OwnedIds
    if ($PortsAfter.Control.Count -ne 1 -or $PortsAfter.Control[0] -ne $Ports.Control[0]) {
        throw 'single-instance activation replaced or duplicated the Control API endpoint'
    }
    $desktopLog = Join-Path $TempRoot 'CFR\logs\desktop.log'
    if ((Test-Path $desktopLog) -and (Select-String -Path $desktopLog -Pattern 'desktop tray startup failed' -Quiet)) {
        throw 'desktop tray startup failed; inspect the isolated desktop.log'
    }

    Stop-IsolatedRun $RootProcess $OwnedIds $TempRoot
    $RootProcess = $null
    $SecondExitCode = $SecondProcess.ExitCode
    $SecondProcess = $null

    $CodexHostAfter = Get-CodexHostShimIds
    if (($CodexHostBefore -join ',') -ne ($CodexHostAfter -join ',')) {
        throw "CodexHost shim set changed during isolated CFR smoke: before=$($CodexHostBefore -join ','), after=$($CodexHostAfter -join ',')"
    }
    $LegacyMarkerAfter = Get-LegacyChatMarkerState
    if ($LegacyMarkerBefore -ne $LegacyMarkerAfter) {
        throw 'real legacy Chat login marker changed during isolated CFR smoke'
    }

    [ordered]@{
        pass = $true
        exe = $ExePath
        sha256 = (Get-FileHash $ExePath -Algorithm SHA256).Hash
        cold_starts = $ColdResults
        control_port = $Ports.Control[0]
        cdp_port = $Ports.Cdp[0]
        close_to_tray = $true
        second_launch_restored_window = $true
        second_exit_code = $SecondExitCode
        visible_terminals = 0
        isolated_orphans = 0
        codexhost_preserved = $true
        legacy_chat_marker_preserved = $true
    } | ConvertTo-Json -Depth 6
} finally {
    if ($RootProcess -and -not $RootProcess.HasExited) {
        try { $OwnedIds = Get-DescendantIds $RootProcess.Id } catch { $OwnedIds = @($RootProcess.Id) }
        Stop-Owned $OwnedIds
    }
    if ($SecondProcess -and -not $SecondProcess.HasExited) {
        Stop-Process -Id $SecondProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    Remove-Item $BaseRoot -Recurse -Force -ErrorAction SilentlyContinue
}
