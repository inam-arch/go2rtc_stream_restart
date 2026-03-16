# start-cameras.ps1
# --------------------------------------------------------------------------
# Starts ffmpeg on Windows to capture from two C922 webcams and push
# RTSP streams into go2rtc running inside Docker.
#
# Usage:
#   .\start-cameras.ps1            # Start both cameras
#   .\start-cameras.ps1 camera1    # Start only camera1
#   .\start-cameras.ps1 camera2    # Start only camera2
#   .\start-cameras.ps1 stop       # Stop all ffmpeg camera processes
#   .\start-cameras.ps1 status     # Show running ffmpeg camera processes
# --------------------------------------------------------------------------

param(
    [string]$Action = "all"
)

$RTSP_SERVER = "rtsp://localhost:8554"
$DEVICE_NAME = "c922 Pro Stream Webcam"

# ffmpeg arguments shared by both cameras
$COMMON_ARGS = @(
    "-f", "dshow",
    "-video_size", "640x480",
    "-framerate", "30"
)
$ENCODE_ARGS = @(
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "zerolatency",
    "-b:v", "1000k",
    "-g", "60",
    "-pix_fmt", "yuv420p",
    "-an",
    "-f", "rtsp",
    "-rtsp_transport", "tcp"
)

function Start-Camera($cameraName, $deviceNumber) {
    $existing = Get-Process ffmpeg -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "$RTSP_SERVER/$cameraName" }
    if ($existing) {
        Write-Host "[SKIP] $cameraName already running (PID $($existing.Id))" -ForegroundColor Yellow
        return
    }

    $argString = "-f dshow -video_size 640x480 -framerate 30 -video_device_number $deviceNumber -i `"video=$DEVICE_NAME`" -c:v libx264 -preset ultrafast -tune zerolatency -b:v 1000k -g 60 -pix_fmt yuv420p -an -f rtsp -rtsp_transport tcp $RTSP_SERVER/$cameraName"

    $proc = Start-Process -FilePath "ffmpeg" `
        -ArgumentList $argString `
        -NoNewWindow `
        -RedirectStandardError "$PSScriptRoot\ffmpeg-$cameraName.log" `
        -PassThru

    if ($proc) {
        Write-Host "[OK] $cameraName started (PID $($proc.Id)) -> $RTSP_SERVER/$cameraName" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] $cameraName failed to start" -ForegroundColor Red
    }
}

function Stop-AllCameras {
    $procs = Get-Process ffmpeg -ErrorAction SilentlyContinue
    if (-not $procs) {
        Write-Host "No ffmpeg processes running" -ForegroundColor Yellow
        return
    }
    $procs | ForEach-Object {
        Write-Host "Stopping ffmpeg PID $($_.Id) ..." -ForegroundColor Cyan
        Stop-Process -Id $_.Id -Force
    }
    Write-Host "[OK] All ffmpeg processes stopped" -ForegroundColor Green
}

function Show-Status {
    Write-Host ""
    Write-Host "=== ffmpeg camera processes ===" -ForegroundColor Yellow
    $procs = Get-Process ffmpeg -ErrorAction SilentlyContinue
    if (-not $procs) {
        Write-Host "  No ffmpeg processes running" -ForegroundColor Red
        return
    }
    $procs | ForEach-Object {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cam = if ($cmd -match "video_device_number.+?0.*$([regex]::Escape($RTSP_SERVER))/camera1") { "camera1" }
               elseif ($cmd -match "video_device_number.+?1.*$([regex]::Escape($RTSP_SERVER))/camera2") { "camera2" }
               elseif ($cmd -match "$([regex]::Escape($RTSP_SERVER))/(\w+)") { $Matches[1] }
               else { "unknown" }
        Write-Host "  $cam : PID $($_.Id)  (running)" -ForegroundColor Green
    }
    Write-Host ""
}

$API = "http://localhost:1984"

function Flush-Stream($cameraName) {
    # Delete the stream to kill all stale producers/consumers,
    # then re-create it empty for the new RTSP push.
    Write-Host "[FLUSH] Clearing stale $cameraName stream ..." -ForegroundColor Cyan
    try {
        Invoke-RestMethod -Uri "$API/api/streams?src=$cameraName" -Method Delete -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
    } catch {}
    # Wait for go2rtc to fully tear down the dead RTSP session
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep 1
        try {
            $r = Invoke-RestMethod -Uri "$API/api/streams" -TimeoutSec 3
            $cam = $r.$cameraName
            if (-not $cam -or -not $cam.producers -or $cam.producers.Count -eq 0) {
                break
            }
        } catch { break }
    }
    # Re-create empty stream
    try {
        Invoke-RestMethod -Uri "$API/api/streams?src=$cameraName" -Method Put -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
    } catch {}
    Write-Host "[FLUSH] $cameraName stream cleared" -ForegroundColor Cyan
}

function Kill-Camera($cameraName) {
    $procs = Get-Process ffmpeg -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -match [regex]::Escape("$RTSP_SERVER/$cameraName")) {
            Write-Host "[KILL] $cameraName (PID $($p.Id))" -ForegroundColor Red
            Stop-Process -Id $p.Id -Force
            Start-Sleep 1
            Flush-Stream $cameraName
            return
        }
    }
    Write-Host "[INFO] $cameraName is not running" -ForegroundColor Yellow
}

function Restart-Camera($cameraName, $deviceNumber) {
    Kill-Camera $cameraName
    Start-Sleep 2
    Start-Camera $cameraName $deviceNumber
}

# --- main ---
switch ($Action) {
    "camera1"          { Start-Camera "camera1" 0 }
    "camera2"          { Start-Camera "camera2" 1 }
    "all"              { Start-Camera "camera1" 0; Start-Camera "camera2" 1 }
    "stop"             { Stop-AllCameras }
    "status"           { Show-Status }
    "kill-camera1"     { Kill-Camera "camera1" }
    "kill-camera2"     { Kill-Camera "camera2" }
    "restart-camera1"  { Restart-Camera "camera1" 0 }
    "restart-camera2"  { Restart-Camera "camera2" 1 }
    default   {
        Write-Host "Usage: .\start-cameras.ps1 <action>" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  all              Start both cameras"
        Write-Host "  camera1          Start camera1 only"
        Write-Host "  camera2          Start camera2 only"
        Write-Host "  status           Show running cameras"
        Write-Host "  kill-camera1     Kill camera1"
        Write-Host "  kill-camera2     Kill camera2"
        Write-Host "  restart-camera1  Kill + restart camera1"
        Write-Host "  restart-camera2  Kill + restart camera2"
        Write-Host "  stop             Stop all cameras"
    }
}
