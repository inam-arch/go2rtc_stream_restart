#!/bin/bash
# start-cameras.sh
# --------------------------------------------------------------------------
# Starts ffmpeg on Linux to capture from two C922 webcams and push
# RTSP streams into go2rtc running inside Docker.
#
# Usage:
#   ./start-cameras.sh            # Start both cameras
#   ./start-cameras.sh camera1    # Start only camera1
#   ./start-cameras.sh camera2    # Start only camera2
#   ./start-cameras.sh stop       # Stop all ffmpeg camera processes
#   ./start-cameras.sh status     # Show running ffmpeg camera processes
# --------------------------------------------------------------------------

RTSP_SERVER="rtsp://localhost:8554"
DEVICE_NAME="c922 Pro Stream Webcam"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API="http://localhost:1984"

start_camera() {
    local camera_name="$1"
    local device_number="$2"

    # Check if already running
    local existing_pid
    existing_pid=$(pgrep -f "ffmpeg.*${RTSP_SERVER}/${camera_name}" 2>/dev/null)
    if [ -n "$existing_pid" ]; then
        echo -e "\e[33m[SKIP] ${camera_name} already running (PID ${existing_pid})\e[0m"
        return
    fi

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

    local pid=$!
    if kill -0 "$pid" 2>/dev/null; then
        echo -e "\e[32m[OK] ${camera_name} started (PID ${pid}) -> ${RTSP_SERVER}/${camera_name}\e[0m"
    else
        echo -e "\e[31m[FAIL] ${camera_name} failed to start\e[0m"
    fi
}

stop_all_cameras() {
    local pids
    pids=$(pgrep -f "ffmpeg.*${RTSP_SERVER}" 2>/dev/null)
    if [ -z "$pids" ]; then
        echo -e "\e[33mNo ffmpeg processes running\e[0m"
        return
    fi
    for pid in $pids; do
        echo -e "\e[36mStopping ffmpeg PID ${pid} ...\e[0m"
        kill -9 "$pid" 2>/dev/null
    done
    echo -e "\e[32m[OK] All ffmpeg processes stopped\e[0m"
}

show_status() {
    echo ""
    echo -e "\e[33m=== ffmpeg camera processes ===\e[0m"
    local pids
    pids=$(pgrep -f "ffmpeg.*${RTSP_SERVER}" 2>/dev/null)
    if [ -z "$pids" ]; then
        echo -e "\e[31m  No ffmpeg processes running\e[0m"
        return
    fi
    for pid in $pids; do
        local cmd
        cmd=$(ps -p "$pid" -o args= 2>/dev/null)
        local cam="unknown"
        if echo "$cmd" | grep -q "${RTSP_SERVER}/camera1"; then
            cam="camera1"
        elif echo "$cmd" | grep -q "${RTSP_SERVER}/camera2"; then
            cam="camera2"
        elif echo "$cmd" | grep -oP "${RTSP_SERVER}/\K\w+" >/dev/null 2>&1; then
            cam=$(echo "$cmd" | grep -oP "${RTSP_SERVER}/\K\w+")
        fi
        echo -e "\e[32m  ${cam} : PID ${pid}  (running)\e[0m"
    done
    echo ""
}

flush_stream() {
    local camera_name="$1"
    echo -e "\e[36m[FLUSH] Skipping stream API mutation for ${camera_name}\e[0m"
}

kill_camera() {
    local camera_name="$1"
    local pids
    pids=$(pgrep -f "ffmpeg.*${RTSP_SERVER}/${camera_name}" 2>/dev/null)
    if [ -n "$pids" ]; then
        for pid in $pids; do
            echo -e "\e[31m[KILL] ${camera_name} (PID ${pid})\e[0m"
            kill -9 "$pid" 2>/dev/null
        done
        sleep 1
        flush_stream "$camera_name"
    else
        echo -e "\e[33m[INFO] ${camera_name} is not running\e[0m"
    fi
}

restart_camera() {
    local camera_name="$1"
    local device_number="$2"
    kill_camera "$camera_name"
    sleep 2
    start_camera "$camera_name" "$device_number"
}

# --- main ---
ACTION="${1:-all}"

case "$ACTION" in
    camera1)          start_camera "camera1" 0 ;;
    camera2)          start_camera "camera2" 2 ;;
    all)              start_camera "camera1" 0; start_camera "camera2" 2 ;;
    stop)             stop_all_cameras ;;
    status)           show_status ;;
    kill-camera1)     kill_camera "camera1" ;;
    kill-camera2)     kill_camera "camera2" ;;
    restart-camera1)  restart_camera "camera1" 0 ;;
    restart-camera2)  restart_camera "camera2" 2 ;;
    *)
        echo -e "\e[33mUsage: ./start-cameras.sh <action>\e[0m"
        echo ""
        echo "  all              Start both cameras"
        echo "  camera1          Start camera1 only"
        echo "  camera2          Start camera2 only"
        echo "  status           Show running cameras"
        echo "  kill-camera1     Kill camera1"
        echo "  kill-camera2     Kill camera2"
        echo "  restart-camera1  Kill + restart camera1"
        echo "  restart-camera2  Kill + restart camera2"
        echo "  stop             Stop all cameras"
        ;;
esac
