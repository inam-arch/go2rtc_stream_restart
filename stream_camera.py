"""
stream_camera.py — Push Windows DirectShow webcams into go2rtc via RTSP.

Enumerate all connected DirectShow video devices and launch one ffmpeg
process per device, pushing to rtsp://localhost:8554/camera<N>.

Run this script first, then run test_display.py to view the streams.

Usage:
    python stream_camera.py

Stop with Ctrl+C — all ffmpeg child processes are killed automatically.
"""

import re
import subprocess
import sys
import time
import signal
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

RTSP_HOST = "localhost"
RTSP_PORT = 8554

# List of (DirectShow device name, go2rtc stream slot).
# For two cameras with the same name (e.g. two identical Logitech C922),
# list the same name twice — stream_camera.py will use -video_device_number
# 0 and 1 to pick each one.
# Run `ffmpeg -f dshow -list_devices true -i dummy` to verify device names.
CAMERA_STREAMS: list[tuple[str, str]] = [
    ("c922 Pro Stream Webcam", "camera1"),
    ("c922 Pro Stream Webcam", "camera2"),
]

# Processes that hold the camera exclusively and must be stopped first.
# LogiFacecamService is the Logi Capture background service on Windows.
CAMERA_HOLDER_PROCESSES = ["LogiFacecamService", "LogiCapture", "LogiVirtualCamera"]

# ffmpeg encoding settings — ultrafast libx264 for minimal latency
FFMPEG_VIDEO_FLAGS = [
    "-vcodec", "libx264",
    "-preset", "ultrafast",
    "-tune",   "zerolatency",
    "-b:v",    "2M",
    "-maxrate","2M",
    "-bufsize", "1M",
    "-pix_fmt","yuv420p",
    "-g",      "30",
    "-an",                  # no audio
]


# ── Kill processes holding cameras ────────────────────────────────────────────

def kill_camera_holders() -> None:
    """Terminate any known processes that exclusively hold camera devices.
    Logi Capture / LogiFacecamService grab the Logitech C922 on startup
    and prevent ffmpeg from opening the device.
    """
    for name in CAMERA_HOLDER_PROCESSES:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", f"{name}.exe"],
            capture_output=True, text=True,
        )
        if "SUCCESS" in result.stdout:
            logger.info("[Stream] Killed camera-holding process: %s", name)
            time.sleep(1)  # give the OS time to release the device


# ── Device enumeration ─────────────────────────────────────────────────────────

def list_dshow_devices() -> list[str]:
    """Return the list of DirectShow video device names as seen by ffmpeg.

    Duplicate device names (two identical cameras) are returned with a '#N'
    suffix appended to distinguish them, e.g.:
        ['c922 Pro Stream Webcam', 'c922 Pro Stream Webcam#1']
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            capture_output=True, text=True, errors="replace",
        )
    except FileNotFoundError:
        logger.error("[Stream] ffmpeg not found on PATH.")
        return []

    devices: list[str] = []
    in_video = False
    for line in result.stderr.splitlines():
        if "DirectShow video devices" in line:
            in_video = True
            continue
        if "DirectShow audio devices" in line:
            in_video = False
            continue
        if not in_video:
            continue
        # Skip "alternative name" lines (contain @device_pnp_ or @device_cm_)
        if "@device_pnp_" in line or "@device_cm_" in line:
            continue
        m = re.search(r'"([^"]+)"', line)
        if m:
            name = m.group(1)
            # Handle duplicate names: append #1, #2, ... for subsequent occurrences
            base = name
            count = devices.count(base) + sum(
                1 for d in devices if d == base or d.startswith(f"{base}#")
            )
            unique = name if count == 0 else f"{name}#{count}"
            devices.append(unique)
    return devices


# ── ffmpeg launcher ────────────────────────────────────────────────────────────

def start_ffmpeg(device_name: str, device_index: int, stream_name: str) -> subprocess.Popen:
    """Launch one ffmpeg process that pushes DirectShow device → go2rtc via RTSP.

    device_index: 0-based index among devices sharing the same name (for
    disambiguating two identical cameras via -video_device_number).
    """
    rtsp_url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{stream_name}"
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-f",  "dshow",
        "-video_device_number", str(device_index),
        "-i",  f"video={device_name}",
        *FFMPEG_VIDEO_FLAGS,
        "-f",  "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_url,
    ]
    logger.info("[Stream] Starting: %s[%d] → %s", device_name, device_index, rtsp_url)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Kill any processes that hold the camera exclusively
    kill_camera_holders()

    # List available DirectShow devices so the user can verify CAMERA_STREAMS
    logger.info("[Stream] Detecting DirectShow video devices ...")
    available = list_dshow_devices()
    if available:
        logger.info("[Stream] Available DirectShow video devices:")
        for d in available:
            logger.info("[Stream]   '%s'", d)
    else:
        logger.warning(
            "[Stream] Could not enumerate DirectShow devices. "
            "Proceeding with CAMERA_STREAMS as configured. "
            "Run: ffmpeg -f dshow -list_devices true -i dummy   to see device names."
        )

    # Build (device_name, device_index, stream_name, proc) list.
    # For duplicate camera names assign incrementing device_index per name.
    name_index_counter: dict[str, int] = {}
    slots: list[tuple[str, int, str]] = []
    for device_name, stream_name in CAMERA_STREAMS:
        idx = name_index_counter.get(device_name, 0)
        name_index_counter[device_name] = idx + 1
        slots.append((device_name, idx, stream_name))

    processes: list[subprocess.Popen] = []
    for device_name, device_index, stream_name in slots:
        proc = start_ffmpeg(device_name, device_index, stream_name)
        processes.append(proc)

    if not processes:
        logger.error("[Stream] No ffmpeg processes started. Check CAMERA_STREAMS.")
        sys.exit(1)

    logger.info("[Stream] %d ffmpeg process(es) running. Press Ctrl+C to stop.", len(processes))

    def _shutdown(sig, frame):
        logger.info("[Stream] Shutting down ffmpeg processes ...")
        for proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        logger.info("[Stream] Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Monitor processes — restart any that die unexpectedly
    while True:
        for i, proc in enumerate(processes):
            ret = proc.poll()
            if ret is not None:
                err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                if err:
                    logger.warning("[Stream] ffmpeg[%d] exited (code %d):\n%s", i, ret, err[-800:])
                device_name, device_index, stream_name = slots[i]
                logger.info("[Stream] Restarting %s[%d] in 3 s ...", device_name, device_index)
                time.sleep(3)
                processes[i] = start_ffmpeg(device_name, device_index, stream_name)
        time.sleep(2)


if __name__ == "__main__":
    main()
