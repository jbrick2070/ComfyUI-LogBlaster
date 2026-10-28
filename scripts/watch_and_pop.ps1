param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [double]$PollSeconds = 1.5
)

$ErrorActionPreference = "SilentlyContinue"
$Url = "http://${HostName}:${Port}/logblaster"
$PromptUrl = "http://${HostName}:${Port}/prompt"
$Launcher = Join-Path (Split-Path -Parent $PSScriptRoot) "launch_log_blaster.bat"

function Test-LogBlasterWindowOpen {
    param([string]$TargetUrl)
    $Procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe' OR Name='chrome.exe'"
    foreach ($P in $Procs) {
        if ($null -ne $P.CommandLine -and $P.CommandLine -like "*$TargetUrl*") {
            return $true
        }
    }
    return $false
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
            if (-not (Test-LogBlasterWindowOpen -TargetUrl $Url)) {
                Write-Host "[LogBlaster] Active workflow detected (queue_remaining=$Remaining)! Launching GPU Shader HUD -> $Url"
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
