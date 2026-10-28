param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [double]$PollSeconds = 1.5
)

$ErrorActionPreference = "SilentlyContinue"
$Url = "http://${HostName}:${Port}/logblaster"
$PromptUrl = "http://${HostName}:${Port}/prompt"
$TelemetryUrl = "http://${HostName}:${Port}/logblaster/telemetry"
$Launcher = Join-Path (Split-Path -Parent $PSScriptRoot) "launch_log_blaster.bat"

function Get-LogBlasterObserverCount {
    $Telem = Invoke-RestMethod -Uri $TelemetryUrl -Method Get -TimeoutSec 2
    if ($null -ne $Telem -and $null -ne $Telem.observers) {
        return [int]($Telem.observers)
    }
    return 0
}

Write-Host "[LogBlaster] Watching $PromptUrl for active ComfyUI runs (Ctrl+C to stop)..."
$WasRunning = $false
$WarnedUnreachable = $false

while ($true) {
    $Resp = Invoke-RestMethod -Uri $PromptUrl -Method Get -TimeoutSec 2
    if ($null -ne $Resp -and $null -ne $Resp.exec_info) {
        $WarnedUnreachable = $false
        $Remaining = [int]($Resp.exec_info.queue_remaining)
        if ($Remaining -gt 0 -and -not $WasRunning) {
            if ((Get-LogBlasterObserverCount) -eq 0) {
                Write-Host "[LogBlaster] Active workflow detected (queue_remaining=$Remaining, observers=0)! Launching GPU Shader HUD -> $Url"
                Start-Process -FilePath $Launcher -ArgumentList "$Port", "$HostName"
            }
            $WasRunning = $true
        } elseif ($Remaining -eq 0) {
            $WasRunning = $false
        }
    } else {
        if (-not $WarnedUnreachable) {
            Write-Host "[LogBlaster] Waiting for ComfyUI server at $PromptUrl ..."
            $WarnedUnreachable = $true
        }
        $WasRunning = $false
    }
    Start-Sleep -Milliseconds ([int]($PollSeconds * 1000))
}
