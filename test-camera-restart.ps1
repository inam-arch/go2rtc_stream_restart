# test-camera-restart.ps1
# --------------------------------------------------------------------------
# Proves that killing ONE camera's ffmpeg process on Windows
# does NOT affect the other camera stream in go2rtc Docker, and that
# the killed camera can be restarted — all without restarting Docker.
#
# Architecture:
#   Windows host: ffmpeg captures from C922 webcams, pushes RTSP to Docker
#   Docker:       go2rtc receives RTSP push, serves WebRTC/HLS/RTSP
#
# Prerequisites: Docker Desktop running, ports 1984/8554/8555 free,
#                two C922 Pro Stream Webcams connected, ffmpeg on PATH.
# Run:  powershell -ExecutionPolicy Bypass -File .\test-camera-restart.ps1
# --------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
$API         = "http://localhost:1984"
$CONTAINER   = "go2rtc"
$RTSP_SERVER = "rtsp://localhost:8554"
$DEVICE_NAME = "c922 Pro Stream Webcam"

# ── helpers ─────────────────────────────────────────────────────────────────

function Write-Pass($m) { Write-Host "[PASS] $m" -ForegroundColor Green }
function Write-Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red }
function Write-Info($m) { Write-Host "[INFO] $m" -ForegroundColor Cyan }
function Write-Step($n,$m) { Write-Host "`n--- Step $n : $m ---" -ForegroundColor Yellow }

function Get-CameraFfmpegPid($cameraName) {
    $procs = Get-Process ffmpeg -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).CommandLine
        if ($cmd -match [regex]::Escape("$RTSP_SERVER/$cameraName")) {
            return $p.Id
        }
    }
    return $null
}

function Start-CameraFfmpeg($cameraName, $deviceNumber) {
    $argString = "-f dshow -video_size 640x480 -framerate 30 -video_device_number $deviceNumber -i `"video=$DEVICE_NAME`" -c:v libx264 -preset ultrafast -tune zerolatency -b:v 1000k -g 60 -pix_fmt yuv420p -an -f rtsp -rtsp_transport tcp $RTSP_SERVER/$cameraName"
    return Start-Process -FilePath "ffmpeg" -ArgumentList $argString -NoNewWindow -RedirectStandardError "$PSScriptRoot\ffmpeg-$cameraName.log" -PassThru
}

function Test-StreamHasProducer($cameraName) {
    try {
        $resp = Invoke-RestMethod -Uri "$API/api/streams" -TimeoutSec 5
        $cam = $resp.$cameraName
        if ($cam -and $cam.producers -and $cam.producers.Count -gt 0) { return $true }
    } catch {}
    return $false
}

# ── preflight ───────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "====================================================" -ForegroundColor Yellow
Write-Host "  go2rtc — Real Webcam Kill / Restart Test"            -ForegroundColor Yellow
Write-Host "  (2x C922 Pro Stream Webcam)"                         -ForegroundColor Yellow
Write-Host "====================================================" -ForegroundColor Yellow

# ── Step 1: start Docker stack ──────────────────────────────────────────────
Write-Step 1 "Start docker compose"
Push-Location $PSScriptRoot
docker compose up -d 2>$null
Pop-Location

# ── Step 2: wait for go2rtc API ─────────────────────────────────────────────
Write-Step 2 "Wait for go2rtc API"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-RestMethod -Uri "$API/api/streams" -TimeoutSec 2 | Out-Null
        $ready = $true; break
    } catch { Start-Sleep 1 }
}
if (-not $ready) { Write-Fail "go2rtc API never became ready"; exit 1 }
Write-Pass "API is up at $API"

# ── Step 3: stop any existing ffmpeg, start both cameras ────────────────────
Write-Step 3 "Start ffmpeg for both cameras (pushing RTSP to go2rtc)"
Get-Process ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force 2>$null
Start-Sleep 1

$proc1 = Start-CameraFfmpeg "camera1" 0
$proc2 = Start-CameraFfmpeg "camera2" 1
Write-Info "Waiting 8 s for ffmpeg to connect and push RTSP ..."
Start-Sleep 8

# ── Step 4: verify both cameras streaming ───────────────────────────────────
Write-Step 4 "Verify both cameras are streaming in go2rtc"
$pid1 = Get-CameraFfmpegPid "camera1"
$pid2 = Get-CameraFfmpegPid "camera2"
$cam1hasProducer = Test-StreamHasProducer "camera1"
$cam2hasProducer = Test-StreamHasProducer "camera2"

$cam1ok = ($null -ne $pid1) -and $cam1hasProducer
$cam2ok = ($null -ne $pid2) -and $cam2hasProducer

if ($cam1ok) { Write-Pass "camera1 streaming  ffmpeg PID=$pid1" } else { Write-Fail "camera1 NOT streaming (PID=$pid1, producer=$cam1hasProducer)" }
if ($cam2ok) { Write-Pass "camera2 streaming  ffmpeg PID=$pid2" } else { Write-Fail "camera2 NOT streaming (PID=$pid2, producer=$cam2hasProducer)" }
if (-not ($cam1ok -and $cam2ok)) { Write-Fail "Cannot continue — both cameras must be streaming"; exit 1 }

# ── Step 5: kill camera1's ffmpeg ───────────────────────────────────────────
Write-Step 5 "KILL camera1 ffmpeg (PID $pid1) — simulating crash"
Stop-Process -Id $pid1 -Force
Start-Sleep 3

# ── Step 6: confirm camera1 is dead ─────────────────────────────────────────
Write-Step 6 "Confirm camera1 is dead"
$pid1after = Get-CameraFfmpegPid "camera1"
$cam1dead = $null -eq $pid1after
$cam1noProducer = -not (Test-StreamHasProducer "camera1")
if ($cam1dead -and $cam1noProducer) { Write-Pass "camera1 is dead (no ffmpeg, no producer)" }
else { Write-Fail "camera1 not fully dead (PID=$pid1after, hasProducer=$(Test-StreamHasProducer 'camera1'))" }

# ── Step 7: KEY TEST — camera2 must still be running ────────────────────────
Write-Step 7 "Confirm camera2 is STILL streaming (must not be affected)"
$pid2after = Get-CameraFfmpegPid "camera2"
$cam2still = ($null -ne $pid2after) -and (Test-StreamHasProducer "camera2")
if ($cam2still) { Write-Pass "camera2 STILL streaming  PID=$pid2after — NOT affected!" }
else            { Write-Fail "camera2 was affected by camera1 kill!" }

# ── Step 8: restart camera1 ────────────────────────────────────────────────
Write-Step 8 "Restart camera1 (start new ffmpeg process)"
$proc1new = Start-CameraFfmpeg "camera1" 0
Write-Info "Waiting 8 s for camera1 to reconnect ..."
Start-Sleep 8

# ── Step 9: verify camera1 is back ──────────────────────────────────────────
Write-Step 9 "Verify camera1 is back"
$pid1new = Get-CameraFfmpegPid "camera1"
$cam1back = ($null -ne $pid1new) -and (Test-StreamHasProducer "camera1")
if ($cam1back) { Write-Pass "camera1 restarted  new PID=$pid1new  (was $pid1)" }
else           { Write-Fail "camera1 did NOT restart" }

# ── Step 10: final check ────────────────────────────────────────────────────
Write-Step 10 "Final check — both cameras healthy"
$pid2final = Get-CameraFfmpegPid "camera2"
$cam2final = ($null -ne $pid2final) -and (Test-StreamHasProducer "camera2")
if ($cam2final) { Write-Pass "camera2 still streaming  PID=$pid2final" }
else            { Write-Fail "camera2 stopped" }

Write-Info "go2rtc API state:"
try { (Invoke-RestMethod -Uri "$API/api/streams" -TimeoutSec 5) | ConvertTo-Json -Depth 3 } catch {}

# ── summary ─────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "====================================================" -ForegroundColor Yellow
Write-Host "  RESULTS" -ForegroundColor Yellow
Write-Host "====================================================" -ForegroundColor Yellow

$allPassed = $cam1ok -and $cam2ok -and $cam1dead -and $cam2still -and $cam1back -and $cam2final

if ($allPassed) {
    Write-Host ""
    Write-Pass "ALL TESTS PASSED"
    Write-Host ""
    Write-Host "  CONCLUSION:" -ForegroundColor Green
    Write-Host "  Killing camera1's ffmpeg on Windows does NOT affect camera2" -ForegroundColor Green
    Write-Host "  in go2rtc Docker. Restarting ffmpeg restores the stream" -ForegroundColor Green
    Write-Host "  without restarting the Docker container." -ForegroundColor Green
    Write-Host ""
    Write-Host "  View streams at: http://localhost:1984" -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host ""
    Write-Fail "SOME TESTS FAILED — see output above"
    Write-Host ""
}
