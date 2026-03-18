"""
stream_camera_linux.py — Push Linux V4L2 webcams into go2rtc via RTSP.

Enumerate all connected V4L2 video devices and launch one ffmpeg
process per device, pushing to rtsp://localhost:8554/camera<N>.

Usage:
    python stream_camera_linux.py

Stop with Ctrl+C — all ffmpeg child processes are killed automatically.

Pre-requisites:
    sudo apt-get install -y ffmpeg v4l-utils
"""

import re
import subprocess
import sys
import time
import signal
import logging
import os
from typing import List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

RTSP_HOST = "localhost"
RTSP_PORT = 8554

# List of (V4L2 device path, stream name).
# Note: On systems with UVC cameras, even-numbered devices (0, 2) are capture devices
# while odd-numbered (1, 3) are metadata devices for the same camera.
CAMERA_STREAMS: List[Tuple[str, str]] = [
    ("/dev/video0", "camera1"),
    ("/dev/video2", "camera2"),
]

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

# Video input device settings
VIDEO_DEVICE_PIXEL_FORMAT = "mjpeg"  # "mjpeg" or "yuyv422" depending on camera
VIDEO_DEVICE_FRAMERATE = "30"
VIDEO_DEVICE_WIDTH = "1280"
VIDEO_DEVICE_HEIGHT = "720"


# ── Device enumeration ─────────────────────────────────────────────────────────

def list_v4l2_devices() -> List[str]:
    """Return the list of V4L2 video devices found on the system."""
    devices: list[str] = []
    for i in range(10):  # Check /dev/video0 through /dev/video9
        dev = f"/dev/video{i}"
        if os.path.exists(dev):
            devices.append(dev)
    return devices


def get_device_info(device: str) -> dict:
    """Get info about a V4L2 device using v4l2-ctl."""
    info = {"device": device, "name": "Unknown", "formats": []}
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device, "--info"],
            capture_output=True, text=True, timeout=5
        )
        if result and result.returncode == 0:
            for line in result.stdout.splitlines():
                if "Driver name" in line or "Card type" in line:
                    info["name"] = line.split(":")[-1].strip()
                    break
    except Exception as e:
        logger.warning(f"Could not query device info for {device}: {e}")
    return info


# ── FFmpeg streaming ──────────────────────────────────────────────────────────

def start_ffmpeg(device: str, stream_name: str) -> subprocess.Popen:
    """Launch an ffmpeg process to stream from a V4L2 device to go2rtc via RTSP."""
    
    rtsp_url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/{stream_name}"
    
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-f", "v4l2",
        "-input_format", VIDEO_DEVICE_PIXEL_FORMAT,
        "-framerate", VIDEO_DEVICE_FRAMERATE,
        "-video_size", f"{VIDEO_DEVICE_WIDTH}x{VIDEO_DEVICE_HEIGHT}",
        "-i", device,
        *FFMPEG_VIDEO_FLAGS,
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_url,
    ]
    
    logger.info(f"[Stream] Starting: {device} → {rtsp_url}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info(f"[Stream] ffmpeg started (PID {proc.pid}) for {stream_name}")
        return proc
    except Exception as e:
        logger.error(f"[Stream] Failed to start ffmpeg: {e}")
        return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    logger.info("[Stream] Detecting V4L2 video devices ...")
    available = list_v4l2_devices()
    if available:
        logger.info("[Stream] Available V4L2 video devices:")
        for dev in available:
            info = get_device_info(dev)
            logger.info(f"[Stream]   {dev}: {info['name']}")
    else:
        logger.warning("[Stream] No V4L2 devices found. Check: ls -la /dev/video*")
    
    # Build list of (device, stream_name) tuples from configuration
    slots: List[Tuple[str, str]] = []
    for device, stream_name in CAMERA_STREAMS:
        if os.path.exists(device):
            slots.append((device, stream_name))
        else:
            logger.warning(f"[Stream] Device {device} not found, skipping")
    
    if not slots:
        logger.error("[Stream] No valid camera streams configured in CAMERA_STREAMS")
        sys.exit(1)
    
    # Start all configured streams
    processes: List[subprocess.Popen] = []
    for device, stream_name in slots:
        proc = start_ffmpeg(device, stream_name)
        if proc:
            processes.append(proc)
    
    if not processes:
        logger.error("[Stream] No ffmpeg processes started")
        sys.exit(1)
    
    logger.info(f"[Stream] {len(processes)} ffmpeg process(es) running. Press Ctrl+C to stop.")
    
    # Shutdown handler
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
                err = ""
                try:
                    if proc.stderr:
                        err = proc.stderr.read().strip()  # Already a string due to text=True
                except Exception:
                    pass
                if err:
                    logger.warning(f"[Stream] ffmpeg[{i}] exited (code {ret}):\n{err[-800:]}")
                device, stream_name = slots[i]
                logger.info(f"[Stream] Restarting {device} in 3 s ...")
                time.sleep(3)
                processes[i] = start_ffmpeg(device, stream_name)
        time.sleep(2)


if __name__ == "__main__":
    main()
