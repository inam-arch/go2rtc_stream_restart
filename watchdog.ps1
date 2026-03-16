# watchdog.ps1
# --------------------------------------------------------------------------
# Monitors camera streams in go2rtc and auto-restarts them if they crash.
# Checks every N seconds if each camera's ffmpeg is running AND go2rtc
# has an active producer. If not, restarts that camera.
#
# Usage:
#   .\watchdog.ps1                     # Check every 15 seconds (default)
#   .\watchdog.ps1 -Interval 30        # Check every 30 seconds
#   .\watchdog.ps1 -MaxRestarts 5      # Stop after 5 restarts per camera
#
# Stop: press Ctrl+C
# --------------------------------------------------------------------------

param(
    [int]$Interval = 3,
    [int]$MaxRestarts = 3
)

$API         = "http://localhost:1984"
$RTSP_SERVER = "rtsp://localhost:8554"
$DEVICE_NAME = "c922 Pro Stream Webcam"

$CAMERAS = @{
    "camera1" = 0
    "camera2" = 1
}

$restartCounts = @{}
foreach ($c in $CAMERAS.Keys) { $restartCounts[$c] = 0 }

function Write-Log($level, $msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    switch ($level) {
        "OK"   { Write-Host "[$ts] [OK]   $msg" -ForegroundColor Green }
        "WARN" { Write-Host "[$ts] [WARN] $msg" -ForegroundColor Yellow }
        "ERR"  { Write-Host "[$ts] [ERR]  $msg" -ForegroundColor Red }
        "INFO" { Write-Host "[$ts] [INFO] $msg" -ForegroundColor Cyan }
    }
}

function Get-FfmpegPid($cameraName) {
    $procs = Get-Process ffmpeg -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -match [regex]::Escape("$RTSP_SERVER/$cameraName")) {
            return $p.Id
        }
    }
    return $null
}

function Test-HasProducer($cameraName) {
    try {
        $r = Invoke-RestMethod -Uri "$API/api/streams" -TimeoutSec 5
        $camData = $r.$cameraName
        if ($camData -and $camData.producers -and $camData.producers.Count -gt 0) { return $true }
    } catch {}
    return $false
}

function Restart-CameraStream($cameraName, $deviceNumber) {
    Write-Log "WARN" "$cameraName is DOWN - restarting"

    $fpid = Get-FfmpegPid $cameraName
    if ($fpid) {
        Stop-Process -Id $fpid -Force -ErrorAction SilentlyContinue
        Write-Log "INFO" "Killed stale ffmpeg PID $fpid"
    }

    try {
        Invoke-RestMethod -Uri "$API/api/streams?src=$cameraName" -Method Delete -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
    } catch {}
    Start-Sleep 2
    try {
        Invoke-RestMethod -Uri "$API/api/streams?src=$cameraName" -Method Put -TimeoutSec 3 -ErrorAction SilentlyContinue | Out-Null
    } catch {}

    $argString = "-f dshow -video_size 640x480 -framerate 30 -video_device_number $deviceNumber -i `"video=$DEVICE_NAME`" -c:v libx264 -preset ultrafast -tune zerolatency -b:v 1000k -g 60 -pix_fmt yuv420p -an -f rtsp -rtsp_transport tcp $RTSP_SERVER/$cameraName"
    $proc = Start-Process -FilePath "ffmpeg" -ArgumentList $argString -NoNewWindow -RedirectStandardError "$PSScriptRoot\ffmpeg-$cameraName.log" -PassThru

    if ($proc) {
        $restartCounts[$cameraName]++
        $cnt = $restartCounts[$cameraName]
        Write-Log "OK" "$cameraName restarted (PID $($proc.Id)) [restarts: $cnt]"
    } else {
        Write-Log "ERR" "$cameraName failed to start!"
    }
}

# --- main ---

Write-Host ""
Write-Host "====================================================" -ForegroundColor Yellow
Write-Host "  go2rtc Camera Watchdog" -ForegroundColor Yellow
$msg = "  Checking every $Interval seconds - Ctrl+C to stop"
Write-Host $msg -ForegroundColor Yellow
Write-Host "====================================================" -ForegroundColor Yellow
Write-Host ""

# Track which cameras were seen healthy at least once (don't restart on first check)
$wasHealthy = @{}
foreach ($c in $CAMERAS.Keys) { $wasHealthy[$c] = $false }

try {
    Invoke-RestMethod -Uri "$API/api/streams" -TimeoutSec 5 | Out-Null
    Write-Log "OK" "go2rtc API reachable at $API"
} catch {
    Write-Log "ERR" "go2rtc API not reachable - is Docker running?"
    Write-Log "INFO" "Run: docker compose up -d"
    exit 1
}

try {
    while ($true) {
        foreach ($cam in $CAMERAS.Keys | Sort-Object) {
            $devNum    = $CAMERAS[$cam]
            $fpid      = Get-FfmpegPid $cam
            $hasProd   = Test-HasProducer $cam

            if ($fpid -and $hasProd) {
                $wasHealthy[$cam] = $true
                Write-Log "OK" "$cam healthy (PID $fpid, producer active)"
            }
            elseif ($fpid -and -not $hasProd) {
                Write-Log "WARN" "$cam ffmpeg running (PID $fpid) but no producer - will recheck"
            }
            elseif (-not $wasHealthy[$cam]) {
                # Never seen healthy - camera was not started yet, skip
                Write-Log "INFO" "$cam not running (start it with: .\start-cameras.ps1 $cam)"
            }
            else {
                # Was healthy before but now dead - auto-restart
                if ($MaxRestarts -gt 0 -and $restartCounts[$cam] -ge $MaxRestarts) {
                    Write-Log "ERR" "$cam hit max restarts ($MaxRestarts) - skipping"
                    continue
                }
                Restart-CameraStream $cam $devNum
            }
        }

        Write-Host ""
        Start-Sleep $Interval
    }
}
finally {
    Write-Host ""
    Write-Log "INFO" "Watchdog stopped"
    Write-Host ""
    Write-Host "=== Restart summary ===" -ForegroundColor Yellow
    foreach ($c in $CAMERAS.Keys | Sort-Object) {
        $cnt = $restartCounts[$c]
        Write-Host ("  {0} : {1} restarts" -f $c, $cnt) -ForegroundColor Cyan
    }
}
