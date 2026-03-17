"""
test.py — Interactive test harness for FrameHandler + FrameHandlerWatchdog.

What it does
------------
1. Starts CameraStreamManager (manages ffmpeg, hotplug, watchdog for go2rtc).
2. Creates a FrameHandlerWatchdog over go2rtc MJPEG URLs.
3. Opens a Tkinter GUI showing:
      - Live frame from each camera (via watchdog.get_frame)
      - Per-camera status tile: URL, frame shape, age of last good frame,
        watchdog handler state (live / dead / recovering)
4. Test buttons per camera:
      - "Kill Handler"  — calls release() on the live FrameHandler so the
                          watchdog detects a crash and restarts it.
      - "Bad URL"       — swaps the stream URL to a broken one so you can
                          watch hotplug-style recovery when you restore it.
      - "Restore URL"   — swaps the URL back to the real go2rtc MJPEG address.

Run:
    python test.py
"""

import io
import logging
import pathlib
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
import requests
from PIL import Image, ImageTk

from frame_handler import FrameHandler, FrameHandlerWatchdog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

GO2RTC_API  = "http://127.0.0.1:1984"
RTSP_BASE   = "rtsp://localhost:8554"

# RTSP URLs for cv2.VideoCapture — cv2 on Windows can't reliably open
# HTTP multipart/MJPEG streams but handles RTSP fine.
CAMERAS: dict[str, str] = {
    "camera1": f"{RTSP_BASE}/camera1",
    "camera2": f"{RTSP_BASE}/camera2",
}

# MJPEG URLs used only for the pre-flight health check via requests
_MJPEG_URLS: dict[str, str] = {
    "camera1": f"{GO2RTC_API}/api/stream.mjpeg?src=camera1",
    "camera2": f"{GO2RTC_API}/api/stream.mjpeg?src=camera2",
}

DISPLAY_W      = 320
DISPLAY_H      = 240
UPDATE_MS      = 66    # ~15 fps — light enough for a test harness
STALE_TIMEOUT  = 10.0  # seconds without frame → watchdog restarts handler
CHECK_INTERVAL = 3.0   # watchdog poll interval


# ── Startup cleanup ────────────────────────────────────────────────────────────

def _kill_stale_ffmpeg() -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["pkill", "-9", "-f", "ffmpeg"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.info("[Test] Stale ffmpeg cleared")
    time.sleep(1)


def _start_camera(name: str) -> None:
    """Launch start-cameras.ps1 to restart a specific camera's ffmpeg."""
    script = str(pathlib.Path(__file__).parent / "start-cameras.ps1")
    action = f"restart-{name}"  # e.g. restart-camera2
    logger.info("[Test] Restarting %s via start-cameras.ps1 %s ...", name, action)
    subprocess.Popen(
        ["powershell", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", script, action],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_cameras() -> None:
    """Launch start-cameras.ps1 to push both webcams into go2rtc via RTSP."""
    script = str(pathlib.Path(__file__).parent / "start-cameras.ps1")
    logger.info("[Test] Starting cameras via start-cameras.ps1 ...")
    subprocess.Popen(
        ["powershell", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)  # give ffmpeg a moment to register with go2rtc


def _stream_has_active_video(name: str, timeout: float = 5.0) -> bool:
    """
    Returns True only when go2rtc is actively sending video frames.
    Reads from the MJPEG endpoint with a short timeout — if we receive any
    bytes the stream is live.  A bare HTTP 200 with no data (no active ffmpeg
    producer) returns False.
    """
    url = f"{GO2RTC_API}/api/stream.mjpeg?src={name}"
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                return False
            for chunk in r.iter_content(chunk_size=512):
                if chunk:
                    return True
        return False
    except Exception:
        return False


# Per-camera timestamp of the last ffmpeg restart to avoid restart spam.
_last_restart: dict[str, float] = {}
_RESTART_COOLDOWN = 30.0  # seconds


def _on_watchdog_fail(name: str) -> None:
    """
    Called every watchdog cycle when a FrameHandler cannot be created.
    1. Skip if we already issued a restart within the cooldown window.
    2. Check for real active video (not just go2rtc config presence).
    3. If no video: restart the specific camera's ffmpeg.
    4. Poll until video is confirmed, then wait for go2rtc RTSP to warm up.
    """
    now = time.monotonic()
    if now - _last_restart.get(name, 0.0) < _RESTART_COOLDOWN:
        return  # cooldown — another restart is already in flight

    logger.warning("[Watchdog] %s handler creation failed — checking for active video ...", name)

    if _stream_has_active_video(name):
        # Video is flowing but RTSP handshake hasn’t fully initialized yet —
        # wait a bit longer and let the next cycle retry _try_create.
        logger.info("[Watchdog] %s has active video — waiting 8 s for RTSP session ...", name)
        time.sleep(8)
        return

    # No active video — ffmpeg is dead.  Restart it.
    logger.warning("[Watchdog] No active video for %s — restarting ffmpeg ...", name)
    _last_restart[name] = now
    _start_camera(name)

    # Block until go2rtc is actually sending video (up to 45 s).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _stream_has_active_video(name):
            logger.info("[Watchdog] %s active — waiting 8 s for RTSP session to warm up ...", name)
            time.sleep(2)
            return
        time.sleep(0.5)
    logger.error("[Watchdog] %s still no active video after 45 s", name)


class TestGUI:
    """
    One tile per camera.  Each tile shows:
      - live video canvas
      - status bar (shape, frame age, handler state)
      - Kill Handler / Bad URL / Restore URL buttons
    """

    def __init__(
        self,
        watchdog: FrameHandlerWatchdog,
        streams: dict[str, str],         # name → real MJPEG URL
    ) -> None:
        self._watchdog  = watchdog
        self._real_urls = dict(streams)  # the "good" URLs for Restore

        self._root      = tk.Tk()
        self._root.title("FrameHandler + Watchdog — Test Harness")
        self._root.configure(bg="#111")
        self._root.resizable(True, True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._canvases:   dict[str, tk.Canvas]           = {}
        self._img_ids:    dict[str, int]                 = {}
        self._photos:     dict[str, ImageTk.PhotoImage]  = {}
        self._status_var: dict[str, tk.StringVar]        = {}
        self._last_frame: dict[str, float]               = {n: time.monotonic() for n in streams}
        self._closing     = False

        self._build_ui(sorted(streams.keys()))

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self, cam_names: list[str]) -> None:
        ncols = max(len(cam_names), 1)
        self._root.geometry(f"{ncols * (DISPLAY_W + 30) + 20}x{DISPLAY_H + 160}")

        for col, name in enumerate(cam_names):
            outer = tk.Frame(self._root, bg="#1e1e1e", bd=1, relief=tk.RIDGE)
            outer.grid(row=0, column=col, padx=8, pady=8, sticky="nsew")
            self._root.columnconfigure(col, weight=1)

            # Camera name header
            tk.Label(outer, text=name, fg="white", bg="#1e1e1e",
                     font=("Segoe UI", 11, "bold")).pack(pady=(4, 2))

            # Video canvas
            canvas = tk.Canvas(outer, width=DISPLAY_W, height=DISPLAY_H,
                                bg="black", highlightthickness=0)
            canvas.pack(padx=4)
            self._canvases[name] = canvas

            # Status text
            var = tk.StringVar(value="Waiting...")
            self._status_var[name] = var
            tk.Label(outer, textvariable=var, fg="#aaa", bg="#1e1e1e",
                     font=("Consolas", 8), justify=tk.LEFT, anchor="w",
                     wraplength=DISPLAY_W).pack(fill=tk.X, padx=6, pady=(2, 4))

            # Buttons
            btn_frame = tk.Frame(outer, bg="#1e1e1e")
            btn_frame.pack(pady=(0, 6))

            tk.Button(
                btn_frame, text="Kill Handler", bg="#b22222", fg="white",
                font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=6,
                command=lambda n=name: self._kill_handler(n),
            ).grid(row=0, column=0, padx=3)

            tk.Button(
                btn_frame, text="Bad URL", bg="#8b6914", fg="white",
                font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=6,
                command=lambda n=name: self._set_bad_url(n),
            ).grid(row=0, column=1, padx=3)

            tk.Button(
                btn_frame, text="Restore URL", bg="#1a6b1a", fg="white",
                font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=6,
                command=lambda n=name: self._restore_url(n),
            ).grid(row=0, column=2, padx=3)

    # ── Tick (frame update loop) ───────────────────────────────────────────────

    def _tick(self) -> None:
        if self._closing:
            return

        for name in self._status_var:
            self._update_tile(name)

        self._root.after(UPDATE_MS, self._tick)

    def _update_tile(self, name: str) -> None:
        fh     = self._watchdog.get_handler(name)
        frame  = self._watchdog.get_frame(name)
        canvas = self._canvases[name]
        now    = time.monotonic()

        # Determine handler state
        if fh is None:
            handler_state = "DEAD / recovering"
        else:
            handler_state = "LIVE"

        if frame is not None:
            self._last_frame[name] = now
            # Render frame — BGR numpy → RGB Pillow
            rgb   = np.ascontiguousarray(frame[:, :, ::-1])
            photo = ImageTk.PhotoImage(
                image=Image.fromarray(rgb).resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
            )
            self._photos[name] = photo
            if name in self._img_ids:
                canvas.itemconfig(self._img_ids[name], image=photo)
            else:
                self._img_ids[name] = canvas.create_image(0, 0, anchor=tk.NW, image=photo)

            age = now - self._last_frame[name]
            shape_str = f"{frame.shape[1]}×{frame.shape[0]}"
            self._status_var[name].set(
                f"Handler: {handler_state}\n"
                f"Shape:   {shape_str}\n"
                f"Age:     {age:.2f}s"
            )
        else:
            age = now - self._last_frame.get(name, now)
            self._status_var[name].set(
                f"Handler: {handler_state}\n"
                f"No frame — stale {age:.1f}s\n"
                f"Watchdog restarts after {STALE_TIMEOUT}s"
            )

    # ── Button actions ─────────────────────────────────────────────────────────

    def _kill_handler(self, name: str) -> None:
        """Force-release the live FrameHandler — the watchdog should recreate it."""
        fh = self._watchdog.get_handler(name)
        if fh is None:
            logger.info("[Test] %s — no live handler to kill", name)
            return
        logger.warning("[Test] Killing FrameHandler for %s (watchdog should restart it)", name)
        try:
            fh.release()
        except Exception:
            pass
        # Force the watchdog's slot to None so it detects the crash immediately
        with self._watchdog._lock:
            self._watchdog._handlers[name] = None
            self._watchdog._last_frame_time[name] = 0.0

    def _set_bad_url(self, name: str) -> None:
        """Swap the URL to a bad address so the next restart attempt fails."""
        bad = "rtsp://localhost:8554/DOES_NOT_EXIST"
        logger.warning("[Test] Setting BAD URL for %s", name)
        with self._watchdog._lock:
            self._watchdog._streams[name] = bad
        self._kill_handler(name)

    def _restore_url(self, name: str) -> None:
        """Swap the URL back to the real go2rtc address."""
        real = self._real_urls[name]
        logger.info("[Test] Restoring real URL for %s -> %s", name, real)
        with self._watchdog._lock:
            self._watchdog._streams[name] = real

    # ── Run / close ───────────────────────────────────────────────────────────

    def run(self) -> None:
        self._tick()
        self._root.mainloop()

    def _on_close(self) -> None:
        self._closing = True
        self._root.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Kill stale ffmpeg
    _kill_stale_ffmpeg()

    # 2. Start cameras (ffmpeg → go2rtc via RTSP)
    _start_cameras()

    # 3. Wait up to 30 s for go2rtc to report active producers
    #    Use _stream_has_active_video — reads actual MJPEG bytes to confirm
    #    ffmpeg is truly streaming (bare HTTP 200 is a false positive).
    logger.info("[Test] Waiting for active video in go2rtc ...")
    deadline = time.monotonic() + 30
    healthy_found = False
    while time.monotonic() < deadline:
        healthy = [name for name in CAMERAS if _stream_has_active_video(name)]
        if healthy:
            logger.info("[Test] Healthy: %s", healthy)
            healthy_found = True
            break
        time.sleep(1)
    if not healthy_found:
        logger.warning("[Test] No active video after 30 s — watchdog will retry")

    # 3. Create FrameHandlerWatchdog
    watchdog = FrameHandlerWatchdog(
        streams=CAMERAS,
        stale_timeout=STALE_TIMEOUT,
        check_interval=CHECK_INTERVAL,
        on_restart=lambda n: logger.info("[Watchdog] %s restarted successfully", n),
        on_fail=_on_watchdog_fail,
    )

    # 4. Open test GUI on main thread
    gui = TestGUI(watchdog=watchdog, streams=CAMERAS)
    try:
        gui.run()
    finally:
        logger.info("[Test] Cleaning up ...")
        watchdog.stop()
        logger.info("[Test] Done")


if __name__ == "__main__":
    main()
