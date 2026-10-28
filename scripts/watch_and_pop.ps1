param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [ValidateRange(0.2, 3600)]
    [double]$PollSeconds = 1.5
)

$Url = "http://${HostName}:${Port}/logblaster"
$PromptUrl = "http://${HostName}:${Port}/prompt"
$TelemetryUrl = "http://${HostName}:${Port}/logblaster/telemetry"
$Launcher = Join-Path (Split-Path -Parent $PSScriptRoot) "launch_log_blaster.bat"

function Get-LogBlasterObserverCount {
    $Telem = $null
    try { $Telem = Invoke-RestMethod -Uri $TelemetryUrl -Method Get -TimeoutSec 2 -ErrorAction Stop } catch {}
    if ($null -ne $Telem -and $null -ne $Telem.observers) {
        return [int]($Telem.observers)
    }
    return 0
}

Write-Host "[LogBlaster] Watching $PromptUrl for active ComfyUI runs (Ctrl+C to stop)..."
$WasRunning = $false
$WarnedUnreachable = $false
$LaunchAttempts = 0
$NextLaunchAt = [DateTime]::MinValue

while ($true) {
    $Resp = $null
    try { $Resp = Invoke-RestMethod -Uri $PromptUrl -Method Get -TimeoutSec 2 -ErrorAction Stop } catch {}
    if ($null -ne $Resp -and $null -ne $Resp.exec_info) {
        $WarnedUnreachable = $false
        $Remaining = [int]($Resp.exec_info.queue_remaining)
        if ($Remaining -gt 0 -and -not $WasRunning) {
            if ((Get-LogBlasterObserverCount) -gt 0) {
                $WasRunning = $true
            } elseif ($LaunchAttempts -lt 3 -and [DateTime]::UtcNow -ge $NextLaunchAt) {
                $LaunchAttempts++
                $NextLaunchAt = [DateTime]::UtcNow.AddSeconds(10)
                Write-Host "[LogBlaster] Active workflow detected (queue_remaining=$Remaining, observers=0)! Launching GPU Shader HUD -> $Url"
                try {
                    Start-Process -FilePath $Launcher -ArgumentList "$Port", "$HostName" -WindowStyle Hidden -ErrorAction Stop
                } catch {
                    Write-Host "[LogBlaster] HUD launch failed; will retry up to three times during this run."
                }
            }
        } elseif ($Remaining -eq 0) {
            $WasRunning = $false
            $LaunchAttempts = 0
            $NextLaunchAt = [DateTime]::MinValue
        }
    } else {
        if (-not $WarnedUnreachable) {
            Write-Host "[LogBlaster] Waiting for ComfyUI server at $PromptUrl ..."
            $WarnedUnreachable = $true
        }
        $WasRunning = $false
        $LaunchAttempts = 0
        $NextLaunchAt = [DateTime]::MinValue
    }
    Start-Sleep -Milliseconds ([int]($PollSeconds * 1000))
}
