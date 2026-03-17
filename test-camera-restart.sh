#!/bin/bash
# test-camera-restart.sh
# --------------------------------------------------------------------------
# Proves that killing ONE camera's ffmpeg process on Linux
# does NOT affect the other camera stream in go2rtc Docker, and that
# the killed camera can be restarted — all without restarting Docker.
#
# Architecture:
#   Linux host: ffmpeg captures from C922 webcams, pushes RTSP to Docker
#   Docker:     go2rtc receives RTSP push, serves WebRTC/HLS/RTSP
#
# Prerequisites: Docker running, ports 1984/8554/8555 free,
#                two C922 Pro Stream Webcams connected, ffmpeg installed.
# Run:  chmod +x test-camera-restart.sh && ./test-camera-restart.sh
# --------------------------------------------------------------------------

set -e

API="http://localhost:1984"
CONTAINER="go2rtc"
RTSP_SERVER="rtsp://localhost:8554"
DEVICE_NAME="c922 Pro Stream Webcam"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── helpers ─────────────────────────────────────────────────────────────────

pass_msg()  { echo -e "\e[32m[PASS] $1\e[0m"; }
fail_msg()  { echo -e "\e[31m[FAIL] $1\e[0m"; }
info_msg()  { echo -e "\e[36m[INFO] $1\e[0m"; }
step_msg()  { echo -e "\n\e[33m--- Step $1 : $2 ---\e[0m"; }

get_camera_ffmpeg_pid() {
    local camera_name="$1"
    pgrep -f "ffmpeg.*${RTSP_SERVER}/${camera_name}" 2>/dev/null | head -1
}

start_camera_ffmpeg() {
    local camera_name="$1"
    local device_number="$2"

    ffmpeg \
        -f v4l2 \
        -video_size 640x480 \
        -framerate 30 \
        -i "/dev/video${device_number}" \
        -c:v libx264 \
        -preset ultrafast \
        -tune zerolatency \
        -b:v 1000k \
        -g 60 \
        -pix_fmt yuv420p \
        -an \
        -f rtsp \
        -rtsp_transport tcp \
        "${RTSP_SERVER}/${camera_name}" \
        2>"${SCRIPT_DIR}/ffmpeg-${camera_name}.log" &
}

test_stream_has_producer() {
    local camera_name="$1"
    local resp
    resp=$(curl -s --max-time 5 "${API}/api/streams" 2>/dev/null || echo "")
    if [ -z "$resp" ]; then
        return 1
    fi
    # Check if stream has producers (non-empty producers array)
    if echo "$resp" | python3 -c "
import sys, json
data = json.load(sys.stdin)
cam = data.get('$camera_name', {})
producers = cam.get('producers') or []
sys.exit(0 if len(producers) > 0 else 1)
" 2>/dev/null; then
        return 0
    fi
    return 1
}

# ── preflight ───────────────────────────────────────────────────────────────

echo ""
echo -e "\e[33m====================================================\e[0m"
echo -e "\e[33m  go2rtc — Real Webcam Kill / Restart Test\e[0m"
echo -e "\e[33m  (2x C922 Pro Stream Webcam)\e[0m"
echo -e "\e[33m====================================================\e[0m"

# ── Step 1: start Docker stack ──────────────────────────────────────────────
step_msg 1 "Start docker compose"
cd "$SCRIPT_DIR"
docker compose up -d 2>/dev/null

# ── Step 2: wait for go2rtc API ─────────────────────────────────────────────
step_msg 2 "Wait for go2rtc API"
ready=false
for i in $(seq 1 30); do
    if curl -s --max-time 2 "${API}/api/streams" >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" = false ]; then
    fail_msg "go2rtc API never became ready"
    exit 1
fi
pass_msg "API is up at ${API}"

# ── Step 3: stop any existing ffmpeg, start both cameras ────────────────────
step_msg 3 "Start ffmpeg for both cameras (pushing RTSP to go2rtc)"
pkill -f "ffmpeg.*${RTSP_SERVER}" 2>/dev/null || true
sleep 1

start_camera_ffmpeg "camera1" 0
start_camera_ffmpeg "camera2" 2
info_msg "Waiting 8 s for ffmpeg to connect and push RTSP ..."
sleep 8

# ── Step 4: verify both cameras streaming ───────────────────────────────────
step_msg 4 "Verify both cameras are streaming in go2rtc"
pid1=$(get_camera_ffmpeg_pid "camera1")
pid2=$(get_camera_ffmpeg_pid "camera2")

cam1_has_producer=false
cam2_has_producer=false
test_stream_has_producer "camera1" && cam1_has_producer=true
test_stream_has_producer "camera2" && cam2_has_producer=true

cam1_ok=false
cam2_ok=false
[ -n "$pid1" ] && [ "$cam1_has_producer" = true ] && cam1_ok=true
[ -n "$pid2" ] && [ "$cam2_has_producer" = true ] && cam2_ok=true

if [ "$cam1_ok" = true ]; then pass_msg "camera1 streaming  ffmpeg PID=${pid1}"; else fail_msg "camera1 NOT streaming (PID=${pid1}, producer=${cam1_has_producer})"; fi
if [ "$cam2_ok" = true ]; then pass_msg "camera2 streaming  ffmpeg PID=${pid2}"; else fail_msg "camera2 NOT streaming (PID=${pid2}, producer=${cam2_has_producer})"; fi
if [ "$cam1_ok" = false ] || [ "$cam2_ok" = false ]; then
    fail_msg "Cannot continue — both cameras must be streaming"
    exit 1
fi

# ── Step 5: kill camera1's ffmpeg ───────────────────────────────────────────
step_msg 5 "KILL camera1 ffmpeg (PID ${pid1}) — simulating crash"
kill -9 "$pid1" 2>/dev/null || true
sleep 3

# ── Step 6: confirm camera1 is dead ─────────────────────────────────────────
step_msg 6 "Confirm camera1 is dead"
pid1_after=$(get_camera_ffmpeg_pid "camera1")
cam1_dead=false
cam1_no_producer=false
[ -z "$pid1_after" ] && cam1_dead=true
! test_stream_has_producer "camera1" && cam1_no_producer=true
if [ "$cam1_dead" = true ] && [ "$cam1_no_producer" = true ]; then
    pass_msg "camera1 is dead (no ffmpeg, no producer)"
else
    fail_msg "camera1 not fully dead (PID=${pid1_after}, hasProducer=$(test_stream_has_producer 'camera1' && echo true || echo false))"
fi

# ── Step 7: KEY TEST — camera2 must still be running ────────────────────────
step_msg 7 "Confirm camera2 is STILL streaming (must not be affected)"
pid2_after=$(get_camera_ffmpeg_pid "camera2")
cam2_still=false
if [ -n "$pid2_after" ] && test_stream_has_producer "camera2"; then
    cam2_still=true
fi
if [ "$cam2_still" = true ]; then
    pass_msg "camera2 STILL streaming  PID=${pid2_after} — NOT affected!"
else
    fail_msg "camera2 was affected by camera1 kill!"
fi

# ── Step 8: restart camera1 ────────────────────────────────────────────────
step_msg 8 "Restart camera1 (start new ffmpeg process)"
start_camera_ffmpeg "camera1" 0
info_msg "Waiting 8 s for camera1 to reconnect ..."
sleep 8

# ── Step 9: verify camera1 is back ──────────────────────────────────────────
step_msg 9 "Verify camera1 is back"
pid1_new=$(get_camera_ffmpeg_pid "camera1")
cam1_back=false
if [ -n "$pid1_new" ] && test_stream_has_producer "camera1"; then
    cam1_back=true
fi
if [ "$cam1_back" = true ]; then
    pass_msg "camera1 restarted  new PID=${pid1_new}  (was ${pid1})"
else
    fail_msg "camera1 did NOT restart"
fi

# ── Step 10: final check ────────────────────────────────────────────────────
step_msg 10 "Final check — both cameras healthy"
pid2_final=$(get_camera_ffmpeg_pid "camera2")
cam2_final=false
if [ -n "$pid2_final" ] && test_stream_has_producer "camera2"; then
    cam2_final=true
fi
if [ "$cam2_final" = true ]; then
    pass_msg "camera2 still streaming  PID=${pid2_final}"
else
    fail_msg "camera2 stopped"
fi

info_msg "go2rtc API state:"
curl -s --max-time 5 "${API}/api/streams" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(could not fetch)"

# ── summary ─────────────────────────────────────────────────────────────────
echo ""
echo -e "\e[33m====================================================\e[0m"
echo -e "\e[33m  RESULTS\e[0m"
echo -e "\e[33m====================================================\e[0m"

all_passed=true
[ "$cam1_ok" = false ] && all_passed=false
[ "$cam2_ok" = false ] && all_passed=false
[ "$cam1_dead" = false ] && all_passed=false
[ "$cam2_still" = false ] && all_passed=false
[ "$cam1_back" = false ] && all_passed=false
[ "$cam2_final" = false ] && all_passed=false

if [ "$all_passed" = true ]; then
    echo ""
    pass_msg "ALL TESTS PASSED"
    echo ""
    echo -e "\e[32m  CONCLUSION:\e[0m"
    echo -e "\e[32m  Killing camera1's ffmpeg on Linux does NOT affect camera2\e[0m"
    echo -e "\e[32m  in go2rtc Docker. Restarting ffmpeg restores the stream\e[0m"
    echo -e "\e[32m  without restarting the Docker container.\e[0m"
    echo ""
    echo -e "\e[36m  View streams at: http://localhost:1984\e[0m"
    echo ""
else
    echo ""
    fail_msg "SOME TESTS FAILED — see output above"
    echo ""
fi
