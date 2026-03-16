#!/bin/sh
# manage-streams.sh — run inside the go2rtc container
# Usage: docker exec go2rtc sh /src/manage-streams.sh <action> [camera]
#
# Actions:
#   list            List all streams and their producer status
#   status          Raw JSON from go2rtc API
#
# NOTE: Camera ffmpeg processes run on the Windows HOST (not in this container).
# Use start-cameras.ps1 on Windows to start/stop/kill cameras.
# This script only queries the go2rtc API for stream status.

API="http://127.0.0.1:1984"

do_list() {
    echo "=== go2rtc stream status ==="
    JSON=$(wget -q -O- "$API/api/streams" 2>/dev/null)
    if [ -z "$JSON" ]; then
        echo "ERROR: API not reachable"
        return 1
    fi
    for cam in camera1 camera2; do
        # Check if stream has producers
        HAS_PRODUCER=$(echo "$JSON" | grep -o "\"$cam\":{\"producers\":\[{" 2>/dev/null)
        if [ -n "$HAS_PRODUCER" ]; then
            echo "$cam : STREAMING (has producer)"
        else
            echo "$cam : NO PRODUCER (ffmpeg not pushing)"
        fi
    done
}

do_status() {
    echo "=== go2rtc API /api/streams ==="
    wget -q -O- "$API/api/streams" 2>/dev/null || echo "ERROR: API not reachable"
}

case "${1:-help}" in
    list)   do_list ;;
    status) do_status ;;
    *)
        echo "Usage: $0 {list|status} [camera]"
        echo ""
        echo "Camera ffmpeg processes run on the Windows HOST."
        echo "Use start-cameras.ps1 on Windows to start/stop/kill cameras."
        exit 1
        ;;
esac
