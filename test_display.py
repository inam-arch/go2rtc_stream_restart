"""
test_display.py — Live stream display GUI.

Displays frames from all cameras managed by a FrameHandlerWatchdog.
No stream-management logic lives here — this file only reads frames
and paints them on screen.

Usage
-----
Configure CAMERAS below to match your go2rtc RTSP (or MJPEG) URLs,
then run:

    python test_display.py

Requirements: pillow, numpy, opencv-python
"""

import os
import re
import time
import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageTk

from frame_handler import FrameHandlerWatchdog, StreamState

# ── Configuration ──────────────────────────────────────────────────────────────

def _rtsp_host() -> str:
    """Return the go2rtc host.
    On Windows: localhost.
    On WSL2 (NAT mode): read Windows host IP from /etc/resolv.conf.
    On WSL2 (mirrored mode): localhost also works.
    """
    if os.path.exists("/etc/resolv.conf") and not os.path.exists("C:/Windows"):
        try:
            with open("/etc/resolv.conf") as fh:
                for line in fh:
                    m = re.match(r"nameserver\s+(\S+)", line)
                    if m:
                        return m.group(1)
        except OSError:
            pass
    return "localhost"

RTSP_BASE = f"rtsp://{_rtsp_host()}:8554"

# RTSP URLs — go2rtc exposes each stream on port 8554/<stream_name>.
# cv2.VideoCapture handles RTSP natively on Windows via the FFmpeg backend.
CAMERAS: dict[str, str] = {
    "camera1": f"{RTSP_BASE}/camera1",
    "camera2": f"{RTSP_BASE}/camera2",
}

DISPLAY_W      = 640    # pixels per video tile
DISPLAY_H      = 480
UPDATE_MS      = 66     # ~15 fps refresh (light on the main thread)
STALE_TIMEOUT  = 5.0   # seconds without a frame → watchdog restarts handler
CHECK_INTERVAL = 2.0    # watchdog polling interval in seconds

# Colour per stream state (shown as a coloured dot next to the state label)
_STATE_COLOUR: dict[StreamState, str] = {
    StreamState.INITIALIZING : "#cccc00",
    StreamState.LIVE         : "#00cc44",
    StreamState.CRASHED      : "#ff6600",
    StreamState.DISCONNECTED : "#cc0000",
    StreamState.RECONNECTING : "#0088ff",
}


# ── GUI ────────────────────────────────────────────────────────────────────────

class StreamDisplayGUI:
    """
    One tile per camera.  Each tile shows:
      - Camera name header
      - Live video canvas (BGR frames converted to RGB for Tkinter)
      - State indicator (coloured dot + state name)
      - Status bar: frame shape and rolling FPS

    All stream management is handled externally by FrameHandlerWatchdog.
    This class only reads frames and state via the public watchdog API.
    """

    def __init__(self, watchdog: FrameHandlerWatchdog) -> None:
        self._watchdog = watchdog
        self._closing  = False

        self._root = tk.Tk()
        self._root.title("Camera Stream Display")
        self._root.configure(bg="#111111")
        self._root.resizable(True, True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Per-camera widgets and bookkeeping
        self._canvases:    dict[str, tk.Canvas]          = {}
        self._img_ids:     dict[str, int]                = {}
        self._photos:      dict[str, ImageTk.PhotoImage] = {}
        self._state_var:   dict[str, tk.StringVar]       = {}
        self._state_lbl:   dict[str, tk.Label]           = {}
        self._status_var:  dict[str, tk.StringVar]       = {}
        # Rolling timestamp list for FPS estimation
        self._frame_times: dict[str, list[float]]        = {}

        self._build_ui(sorted(watchdog.camera_names()))

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self, cam_names: list[str]) -> None:
        ncols = max(len(cam_names), 1)
        win_w = ncols * (DISPLAY_W + 20) + 20
        win_h = DISPLAY_H + 110
        self._root.geometry(f"{win_w}x{win_h}")

        for col, name in enumerate(cam_names):
            self._frame_times[name] = []

            outer = tk.Frame(self._root, bg="#1e1e1e", bd=1, relief=tk.RIDGE)
            outer.grid(row=0, column=col, padx=8, pady=8, sticky="nsew")
            self._root.columnconfigure(col, weight=1)

            # Camera name header
            tk.Label(
                outer, text=name, fg="white", bg="#1e1e1e",
                font=("Segoe UI", 11, "bold"),
            ).pack(pady=(6, 2))

            # Video canvas
            canvas = tk.Canvas(
                outer, width=DISPLAY_W, height=DISPLAY_H,
                bg="black", highlightthickness=0,
            )
            canvas.pack(padx=4)
            self._canvases[name] = canvas

            # State indicator line
            state_var = tk.StringVar(value="● INITIALIZING")
            self._state_var[name] = state_var
            state_lbl = tk.Label(
                outer, textvariable=state_var,
                fg=_STATE_COLOUR[StreamState.INITIALIZING],
                bg="#1e1e1e", font=("Segoe UI", 9, "bold"),
            )
            state_lbl.pack(pady=(4, 0))
            self._state_lbl[name] = state_lbl

            # Status bar (shape + FPS)
            status_var = tk.StringVar(value="Waiting for frames…")
            self._status_var[name] = status_var
            tk.Label(
                outer, textvariable=status_var,
                fg="#aaaaaa", bg="#1e1e1e",
                font=("Consolas", 8), justify=tk.LEFT, anchor="w",
                wraplength=DISPLAY_W,
            ).pack(fill=tk.X, padx=6, pady=(2, 8))

    # ── Frame refresh loop ─────────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._closing:
            return
        for name in self._state_var:
            self._update_tile(name)
        self._root.after(UPDATE_MS, self._tick)

    def _update_tile(self, name: str) -> None:
        state  = self._watchdog.get_state(name)
        frame  = self._watchdog.get_frame(name)
        canvas = self._canvases[name]
        now    = time.monotonic()

        # ── state indicator ────────────────────────────────────────────────
        colour = _STATE_COLOUR.get(state, "#888888")
        self._state_var[name].set(f"● {state.value.upper()}")
        self._state_lbl[name].config(fg=colour)

        # ── video frame ────────────────────────────────────────────────────
        if frame is not None:
            # Rolling FPS (last 30 frames)
            times = self._frame_times[name]
            times.append(now)
            if len(times) > 30:
                times.pop(0)
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0

            # BGR (OpenCV) → RGB (Pillow) → resize → Tk PhotoImage
            rgb   = np.ascontiguousarray(frame[:, :, ::-1])
            photo = ImageTk.PhotoImage(
                Image.fromarray(rgb).resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
            )
            # Keep a reference — Tkinter does NOT keep it alive otherwise
            self._photos[name] = photo

            if name in self._img_ids:
                canvas.itemconfig(self._img_ids[name], image=photo)
            else:
                self._img_ids[name] = canvas.create_image(0, 0, anchor=tk.NW, image=photo)

            h, w = frame.shape[:2]
            self._status_var[name].set(f"Shape: {w}×{h}    FPS: {fps:.1f}")
        else:
            self._status_var[name].set("No frame — waiting for stream…")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the Tkinter event loop (blocks until the window is closed)."""
        self._tick()
        self._root.mainloop()

    def _on_close(self) -> None:
        self._closing = True
        self._root.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    watchdog = FrameHandlerWatchdog(
        streams=CAMERAS,
        stale_timeout=STALE_TIMEOUT,
        check_interval=CHECK_INTERVAL,
    )
    gui = StreamDisplayGUI(watchdog=watchdog)
    try:
        gui.run()
    finally:
        watchdog.stop()


if __name__ == "__main__":
    main()
