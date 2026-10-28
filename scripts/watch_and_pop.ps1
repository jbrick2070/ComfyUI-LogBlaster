param(
    [int]$Port = 8000,
    [double]$PollSeconds = 1.5
)

$ErrorActionPreference = "SilentlyContinue"
$Url = "http://127.0.0.1:$Port/logblaster"
$QueueUrl = "http://127.0.0.1:$Port/queue"
$Launcher = Join-Path (Split-Path -Parent $PSScriptRoot) "launch_log_blaster.bat"

Write-Host "[LogBlaster] Watching $QueueUrl for active ComfyUI runs (Ctrl+C to stop)..."
$WasRunning = $false

while ($true) {
    $Resp = Invoke-RestMethod -Uri $QueueUrl -Method Get -TimeoutSec 2
    if ($null -ne $Resp -and $null -ne $Resp.queue_running) {
        $RunningCount = @($Resp.queue_running).Count
        if ($RunningCount -gt 0 -and -not $WasRunning) {
            Write-Host "[LogBlaster] Active workflow detected! Launching GPU Shader HUD -> $Url"
            Start-Process -FilePath $Launcher -ArgumentList "$Port"
            $WasRunning = $true
        } elseif ($RunningCount -eq 0) {
            $WasRunning = $false
        }
    } else {
        $WasRunning = $false
    }
    Start-Sleep -Milliseconds ([int]($PollSeconds * 1000))
}
