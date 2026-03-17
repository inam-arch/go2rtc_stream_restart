"""
stream_camera_wsl.py — Read go2rtc camera streams from WSL.

Flow:
    Webcam (Windows) → ffmpeg (Windows) → RTSP push → go2rtc (Docker)
                                                            ↓
                                          stream_camera_wsl.py (WSL) reads RTSP

No USB passthrough or v4l2 devices needed. The camera streams are already
served by go2rtc running in Docker on the Windows host. This script pulls
them from WSL via RTSP and reports frame stats (resolution, FPS) to confirm
the streams are live and reachable.

Pre-requisites (WSL, run once):
    sudo apt-get update && sudo apt-get install -y python3-opencv python3-pip
    pip install opencv-python-headless

Usage (inside WSL):
    python stream_camera_wsl.py

Configuration:
    Edit RTSP_HOST_MODE and CAMERA_NAMES below.
"""

import re
import signal
import sys
import threading
import time
import logging

import cv2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

RTSP_PORT = 8554

# "auto"      → read Windows host IP from /etc/resolv.conf (standard WSL2 NAT)
# "localhost" → for WSL2 mirrored-networking mode (.wslconfig networkingMode=mirrored)
# "x.x.x.x"  → fixed IP or hostname
RTSP_HOST_MODE = "auto"

# Stream names as configured in go2rtc.yaml
CAMERA_NAMES: list[str] = ["camera1", "camera2"]

# How often to log frame stats (seconds)
STATS_INTERVAL = 5.0


# ── Windows-host IP detection ──────────────────────────────────────────────────

def get_rtsp_host() -> str:
    if RTSP_HOST_MODE == "localhost":
        return "localhost"
    if RTSP_HOST_MODE != "auto":
        return RTSP_HOST_MODE

    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                m = re.match(r"nameserver\s+(\S+)", line)
                if m:
                    ip = m.group(1)
                    logger.info("[WSL] Windows host IP detected: %s", ip)
                    return ip
    except OSError:
        pass

    logger.warning("[WSL] Could not read /etc/resolv.conf — falling back to 'localhost'")
    return "localhost"


# ── Per-stream reader ──────────────────────────────────────────────────────────

class StreamReader:
    """Opens an RTSP stream from go2rtc and reads frames in a background thread."""

    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self.url  = url
        self._running = True
        self._frame_count = 0
        self._last_stats  = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"reader-{name}")
        self._thread.start()

    def _run(self) -> None:
        logger.info("[WSL] [%s] Connecting to %s …", self.name, self.url)
        while self._running:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                logger.warning("[WSL] [%s] Could not open stream — retrying in 3 s …", self.name)
                time.sleep(3)
                continue

            w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            logger.info("[WSL] [%s] Stream open: %dx%d @ %.1f fps", self.name, w, h, fps)

            try:
                while self._running:
                    ok, _ = cap.read()
                    if not ok:
                        logger.warning("[WSL] [%s] Frame read failed — reconnecting …", self.name)
                        break
                    self._frame_count += 1

                    now = time.monotonic()
                    if now - self._last_stats >= STATS_INTERVAL:
                        elapsed = now - self._last_stats
                        measured_fps = self._frame_count / elapsed
                        logger.info(
                            "[WSL] [%s] Live — %dx%d  measured FPS: %.1f",
                            self.name, w, h, measured_fps,
                        )
                        self._frame_count = 0
                        self._last_stats  = now
            finally:
                cap.release()

            if self._running:
                time.sleep(2)

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=5)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    rtsp_host = get_rtsp_host()
    logger.info("[WSL] go2rtc host: %s  port: %d", rtsp_host, RTSP_PORT)
    logger.info("[WSL] Streams to read: %s", CAMERA_NAMES)

    readers: list[StreamReader] = []
    for name in CAMERA_NAMES:
        url = f"rtsp://{rtsp_host}:{RTSP_PORT}/{name}"
        readers.append(StreamReader(name, url))

    def _shutdown(sig, frame):
        logger.info("[WSL] Stopping …")
        for r in readers:
            r.stop()
        logger.info("[WSL] Done.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("[WSL] Reading %d stream(s). Press Ctrl+C to stop.", len(readers))
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
