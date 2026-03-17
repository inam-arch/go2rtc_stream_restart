#!/bin/bash
# watchdog.sh
# --------------------------------------------------------------------------
# Monitors camera streams in go2rtc and auto-restarts them if they crash.
# Checks every N seconds if each camera's ffmpeg is running AND go2rtc
# has an active producer. If not, restarts that camera.
#
# Usage:
#   ./watchdog.sh                              # Defaults
#   ./watchdog.sh --interval 30                # Check every 30 seconds
#   ./watchdog.sh --max-restarts 5             # Stop after 5 restarts per camera
#   ./watchdog.sh --no-auto-start              # Only restart previously healthy cameras
#   ./watchdog.sh --unhealthy-checks 3         # Unhealthy checks before restart
#
# Stop: press Ctrl+C
# --------------------------------------------------------------------------

# ── defaults ────────────────────────────────────────────────────────────────
INTERVAL=3
MAX_RESTARTS=3
UNHEALTHY_CHECKS_BEFORE_RESTART=2
HEALTHY_CHECKS_BEFORE_RESET=5
AUTO_START_MISSING=true

# ── parse arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval)             INTERVAL="$2"; shift 2 ;;
        --max-restarts)         MAX_RESTARTS="$2"; shift 2 ;;
        --unhealthy-checks)     UNHEALTHY_CHECKS_BEFORE_RESTART="$2"; shift 2 ;;
        --healthy-checks)       HEALTHY_CHECKS_BEFORE_RESET="$2"; shift 2 ;;
        --no-auto-start)        AUTO_START_MISSING=false; shift ;;
        *)                      echo "Unknown option: $1"; exit 1 ;;
    esac
done

API="http://localhost:1984"
RTSP_SERVER="rtsp://localhost:8554"
DEVICE_NAME="c922 Pro Stream Webcam"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Camera name -> device number mapping
declare -A CAMERAS
CAMERAS[camera1]=0
CAMERAS[camera2]=2

# Per-camera counters
declare -A restart_counts
declare -A unhealthy_streak
declare -A healthy_streak
declare -A was_healthy

for cam in "${!CAMERAS[@]}"; do
    restart_counts[$cam]=0
    unhealthy_streak[$cam]=0
    healthy_streak[$cam]=0
    was_healthy[$cam]=false
done

# ── helpers ─────────────────────────────────────────────────────────────────

log_msg() {
    local level="$1"
    local msg="$2"
    local ts
    ts=$(date "+%Y-%m-%d %H:%M:%S")
    case "$level" in
        OK)   echo -e "[\e[32m${ts}\e[0m] [\e[32mOK\e[0m]   ${msg}" ;;
        WARN) echo -e "[\e[33m${ts}\e[0m] [\e[33mWARN\e[0m] ${msg}" ;;
        ERR)  echo -e "[\e[31m${ts}\e[0m] [\e[31mERR\e[0m]  ${msg}" ;;
        INFO) echo -e "[\e[36m${ts}\e[0m] [\e[36mINFO\e[0m] ${msg}" ;;
    esac
}

get_ffmpeg_pid() {
    local camera_name="$1"
    pgrep -f "ffmpeg.*${RTSP_SERVER}/${camera_name}" 2>/dev/null | head -1
}

test_has_producer() {
    local camera_name="$1"
    local resp
    resp=$(curl -s --max-time 5 "${API}/api/streams" 2>/dev/null || echo "")
    if [ -z "$resp" ]; then
        return 1
    fi
    if echo "$resp" | python3 -c "
import sys, json
data = json.load(sys.stdin)
cam = data.get('$camera_name', {})
producers = cam.get('producers') or []
for p in producers:
    if p.get('id') or p.get('remote_addr') or p.get('format_name'):
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
        return 0
    fi
    return 1
}

restart_camera_stream() {
    local camera_name="$1"
    local device_number="$2"

    log_msg "WARN" "${camera_name} is DOWN - restarting"

    local fpid
    fpid=$(get_ffmpeg_pid "$camera_name")
    if [ -n "$fpid" ]; then
        kill -9 "$fpid" 2>/dev/null || true
        log_msg "INFO" "Killed stale ffmpeg PID ${fpid}"
    fi

    sleep 2

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

    local new_pid=$!
    if kill -0 "$new_pid" 2>/dev/null; then
        restart_counts[$camera_name]=$(( ${restart_counts[$camera_name]} + 1 ))
        local cnt=${restart_counts[$camera_name]}
        log_msg "OK" "${camera_name} restarted (PID ${new_pid}) [restarts: ${cnt}]"
    else
        log_msg "ERR" "${camera_name} failed to start!"
    fi
}

# ── main ────────────────────────────────────────────────────────────────────

echo ""
echo -e "\e[33m====================================================\e[0m"
echo -e "\e[33m  go2rtc Camera Watchdog\e[0m"
echo -e "\e[33m  Checking every ${INTERVAL} seconds - Ctrl+C to stop\e[0m"
echo -e "\e[33m====================================================\e[0m"
echo ""

# Check API reachability
if curl -s --max-time 5 "${API}/api/streams" >/dev/null 2>&1; then
    log_msg "OK" "go2rtc API reachable at ${API}"
else
    log_msg "ERR" "go2rtc API not reachable - is Docker running?"
    log_msg "INFO" "Run: docker compose up -d"
    exit 1
fi

cleanup() {
    echo ""
    log_msg "INFO" "Watchdog stopped"
    echo ""
    echo -e "\e[33m=== Restart summary ===\e[0m"
    for cam in $(echo "${!CAMERAS[@]}" | tr ' ' '\n' | sort); do
        local cnt=${restart_counts[$cam]}
        echo -e "\e[36m  ${cam} : ${cnt} restarts\e[0m"
    done
}
trap cleanup EXIT

while true; do
    for cam in $(echo "${!CAMERAS[@]}" | tr ' ' '\n' | sort); do
        dev_num=${CAMERAS[$cam]}
        fpid=$(get_ffmpeg_pid "$cam")
        has_prod=false
        test_has_producer "$cam" && has_prod=true

        if [ -n "$fpid" ] && [ "$has_prod" = true ]; then
            # Healthy
            was_healthy[$cam]=true
            unhealthy_streak[$cam]=0
            healthy_streak[$cam]=$(( ${healthy_streak[$cam]} + 1 ))

            if [ "${restart_counts[$cam]}" -gt 0 ] && [ "${healthy_streak[$cam]}" -ge "$HEALTHY_CHECKS_BEFORE_RESET" ]; then
                restart_counts[$cam]=0
                log_msg "INFO" "${cam} stable for ${healthy_streak[$cam]} checks - restart counter reset"
            fi
            log_msg "OK" "${cam} healthy (PID ${fpid}, producer active)"

        elif [ -n "$fpid" ] && [ "$has_prod" = false ]; then
            # ffmpeg running but no producer
            healthy_streak[$cam]=0
            unhealthy_streak[$cam]=$(( ${unhealthy_streak[$cam]} + 1 ))
            local streak=${unhealthy_streak[$cam]}

            if [ "$streak" -ge "$UNHEALTHY_CHECKS_BEFORE_RESTART" ]; then
                if [ "$MAX_RESTARTS" -gt 0 ] && [ "${restart_counts[$cam]}" -ge "$MAX_RESTARTS" ]; then
                    log_msg "ERR" "${cam} hit max restarts (${MAX_RESTARTS}) - skipping"
                    continue
                fi
                log_msg "WARN" "${cam} ffmpeg running (PID ${fpid}) but no producer for ${streak} checks - restarting"
                restart_camera_stream "$cam" "$dev_num"
                unhealthy_streak[$cam]=0
            else
                log_msg "WARN" "${cam} ffmpeg running (PID ${fpid}) but no producer - recheck ${streak}/${UNHEALTHY_CHECKS_BEFORE_RESTART}"
            fi

        elif [ "${was_healthy[$cam]}" = false ]; then
            # Never been healthy
            healthy_streak[$cam]=0
            if [ "$AUTO_START_MISSING" = true ]; then
                if [ "$MAX_RESTARTS" -gt 0 ] && [ "${restart_counts[$cam]}" -ge "$MAX_RESTARTS" ]; then
                    log_msg "ERR" "${cam} hit max restarts (${MAX_RESTARTS}) - skipping"
                    continue
                fi
                log_msg "WARN" "${cam} not running and never healthy - auto-starting"
                restart_camera_stream "$cam" "$dev_num"
                unhealthy_streak[$cam]=0
            else
                log_msg "INFO" "${cam} not running (start it with: ./start-cameras.sh ${cam})"
            fi

        else
            # Was healthy but now dead
            healthy_streak[$cam]=0
            if [ "$MAX_RESTARTS" -gt 0 ] && [ "${restart_counts[$cam]}" -ge "$MAX_RESTARTS" ]; then
                log_msg "ERR" "${cam} hit max restarts (${MAX_RESTARTS}) - skipping"
                continue
            fi
            restart_camera_stream "$cam" "$dev_num"
            unhealthy_streak[$cam]=0
        fi
    done

    echo ""
    sleep "$INTERVAL"
done
