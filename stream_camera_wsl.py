"""
stream_camera_wsl.py — Push WSL v4l2 webcams into go2rtc via RTSP.

Pre-requisites
--------------
Windows (run once, as Administrator):
    powershell -ExecutionPolicy Bypass -File setup_usbipd.ps1

WSL (run once):
    sudo apt-get update
    sudo apt-get install -y linux-tools-generic hwdata v4l-utils ffmpeg
    sudo update-alternatives --install /usr/local/bin/usbip usbip \\
        /usr/lib/linux-tools/*-generic/usbip 20

Usage (inside WSL):
    python stream_camera_wsl.py

The script:
  1. Auto-detects the Windows host IP from /etc/resolv.conf so it can reach
     go2rtc running via Docker Desktop on Windows.
  2. Enumerates v4l2 capture nodes via `v4l2-ctl --list-devices`.
  3. Launches one ffmpeg process per camera:
         /dev/videoN  →  rtsp://<windows-host>:8554/cameraN
  4. Monitors and restarts ffmpeg on crash.
  5. Exits cleanly on Ctrl-C / SIGTERM.

Configuration
-------------
Edit RTSP_HOST_MODE and CAMERA_STREAMS below as needed.
"""

import os
import re
import signal
import subprocess
import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

RTSP_PORT = 8554

# "auto"      → read Windows host IP from /etc/resolv.conf  (standard WSL2 NAT)
# "localhost" → for WSL2 mirrored-networking mode (Windows 11, .wslconfig)
# "x.x.x.x"  → fixed IP / hostname
RTSP_HOST_MODE = "auto"

# Optional manual mapping: {"/dev/video0": "camera1", "/dev/video2": "camera2"}
# Leave empty — cameras are auto-assigned camera1, camera2, … in order.
DEVICE_OVERRIDE: dict[str, str] = {}

# ffmpeg encoding flags — ultrafast libx264, low-latency, no audio
FFMPEG_VIDEO_FLAGS = [
    "-vcodec", "libx264",
    "-preset", "ultrafast",
    "-tune",   "zerolatency",
    "-b:v",    "2M",
    "-maxrate", "2M",
    "-bufsize", "1M",
    "-pix_fmt", "yuv420p",
    "-g",       "30",
    "-an",
]


# ── Windows-host IP detection ──────────────────────────────────────────────────

def get_rtsp_host() -> str:
    """Return the IP/hostname to use for go2rtc's RTSP server."""
    if RTSP_HOST_MODE == "localhost":
        return "localhost"
    if RTSP_HOST_MODE != "auto":
        return RTSP_HOST_MODE  # treat as a literal IP or hostname

    # Standard WSL2 NAT: the Windows host is the nameserver in /etc/resolv.conf
    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                m = re.match(r"nameserver\s+(\S+)", line)
                if m:
                    ip = m.group(1)
                    logger.info("[WSL] Windows host IP: %s", ip)
                    return ip
    except OSError:
        pass

    logger.warning("[WSL] Could not read /etc/resolv.conf — falling back to 'localhost'")
    return "localhost"


# ── v4l2 device enumeration ────────────────────────────────────────────────────

def list_v4l2_capture_devices() -> list[tuple[str, str]]:
    """
    Return [(device_label, '/dev/videoN'), ...] — one entry per physical camera.

    Uses `v4l2-ctl --list-devices` and picks the FIRST /dev/videoN per device
    (the second node per camera is usually a metadata / ISP node, not capture).

    Falls back to a raw /dev/video* scan if v4l2-ctl is unavailable.
    """
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=10,
        )
        cameras: list[tuple[str, str]] = []
        current_label: str = "unknown"
        picked_first = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not line.startswith("\t") and stripped:
                # Header: "C922 Pro Stream Webcam (usb-0000:00:14.0-1):"
                current_label = stripped.split("(")[0].strip().rstrip(":")
                picked_first = False
            elif stripped.startswith("/dev/video") and not picked_first:
                cameras.append((current_label, stripped))
                picked_first = True
        if cameras:
            return cameras
        logger.warning("[WSL] v4l2-ctl --list-devices returned no devices")
    except FileNotFoundError:
        logger.warning("[WSL] v4l2-ctl not found — falling back to /dev/video* scan")
    except subprocess.TimeoutExpired:
        logger.warning("[WSL] v4l2-ctl timed out — falling back to /dev/video* scan")

    # Fallback: check /dev/video* for Video Capture capability
    cameras = []
    try:
        nodes = sorted(
            f"/dev/{n}" for n in os.listdir("/dev") if n.startswith("video")
        )
    except OSError:
        nodes = []

    for devpath in nodes:
        label = devpath
        try:
            info = subprocess.run(
                ["v4l2-ctl", "-d", devpath, "--info"],
                capture_output=True, text=True, timeout=5,
            )
            if "Video Capture" in info.stdout:
                cameras.append((label, devpath))
        except Exception:
            cameras.append((label, devpath))  # include unconditionally

    return cameras


# ── dependency check ───────────────────────────────────────────────────────────

def check_dependencies() -> bool:
    ok = True
    for cmd, pkg in [("v4l2-ctl", "v4l-utils"), ("ffmpeg", "ffmpeg")]:
        if subprocess.run(["which", cmd], capture_output=True).returncode != 0:
            logger.error(
                "[WSL] '%s' not found. Install: sudo apt-get install -y %s",
                cmd, pkg,
            )
            ok = False
    return ok


# ── ffmpeg launcher ────────────────────────────────────────────────────────────

def start_ffmpeg(device_path: str, stream_name: str, rtsp_host: str) -> subprocess.Popen:
    """Launch ffmpeg: v4l2 capture node → go2rtc RTSP push."""
    rtsp_url = f"rtsp://{rtsp_host}:{RTSP_PORT}/{stream_name}"
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-f", "v4l2",
        "-i", device_path,
        *FFMPEG_VIDEO_FLAGS,
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_url,
    ]
    logger.info("[WSL] Starting: %s → %s", device_path, rtsp_url)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if not check_dependencies():
        logger.error(
            "[WSL] Missing dependencies. Install them and re-run.\n"
            "      sudo apt-get install -y v4l-utils ffmpeg"
        )
        sys.exit(1)

    logger.info("[WSL] Scanning v4l2 video capture devices …")
    cameras = list_v4l2_capture_devices()

    if not cameras:
        logger.error(
            "[WSL] No v4l2 capture devices found.\n"
            "      1. On Windows (as Administrator): powershell -ExecutionPolicy Bypass -File setup_usbipd.ps1\n"
            "      2. In WSL: ls /dev/video*   (should list /dev/video0, /dev/video2 …)\n"
            "      3. If no /dev/video* nodes exist, the USB device was not attached.\n"
            "         Re-run setup_usbipd.ps1 with a WSL terminal open."
        )
        sys.exit(1)

    logger.info("[WSL] Detected %d camera(s):", len(cameras))
    for label, path in cameras:
        logger.info("[WSL]   %-16s  %s", path, label)

    rtsp_host = get_rtsp_host()

    # Build (device_path, stream_name) slots
    slots: list[tuple[str, str]] = []
    for i, (_, dev_path) in enumerate(cameras, start=1):
        stream_name = DEVICE_OVERRIDE.get(dev_path, f"camera{i}")
        slots.append((dev_path, stream_name))
        logger.info("[WSL] %s → rtsp://%s:%d/%s", dev_path, rtsp_host, RTSP_PORT, stream_name)

    processes: list[subprocess.Popen] = []
    for dev_path, stream_name in slots:
        proc = start_ffmpeg(dev_path, stream_name, rtsp_host)
        processes.append(proc)

    logger.info("[WSL] %d ffmpeg process(es) running. Press Ctrl+C to stop.", len(processes))

    def _shutdown(sig, frame):
        logger.info("[WSL] Shutting down ffmpeg processes …")
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
        logger.info("[WSL] Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Monitor loop — restart any crashed ffmpeg
    while True:
        for i, proc in enumerate(processes):
            ret = proc.poll()
            if ret is not None:
                err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                if err:
                    logger.warning("[WSL] ffmpeg[%d] exited (code %d):\n%s", i, ret, err[-800:])
                dev_path, stream_name = slots[i]
                logger.info("[WSL] Restarting %s in 3 s …", dev_path)
                time.sleep(3)
                processes[i] = start_ffmpeg(dev_path, stream_name, rtsp_host)
        time.sleep(2)


if __name__ == "__main__":
    main()
