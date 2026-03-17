"""
stream_camera.py — Pure display GUI for camera streams.

Only displays frames from existing FrameHandlers — does NOT manage
streams, ffmpeg, or HID. All of that is handled by main.py.

Usage from main.py:
    from stream_camera import StreamGUI
    gui = StreamGUI(frame_handlers)
    gui.start()       # blocking — runs tkinter mainloop
    # or
    gui.start_background()  # non-blocking — runs in a thread
"""

import logging
import threading
import tkinter as tk

import cv2
from PIL import Image, ImageTk

from frame_handler import FrameHandler

logger = logging.getLogger(__name__)

DISPLAY_W = 320
DISPLAY_H = 240
UPDATE_MS = 33  # ~30 fps


class StreamGUI:
    """Displays frames from FrameHandlers in a Tkinter window."""

    def __init__(self, frame_handlers: dict[str, FrameHandler]):
        self.frame_handlers = frame_handlers
        self._root: tk.Tk | None = None
        self._panels: dict[str, tk.Canvas] = {}
        self._status_labels: dict[str, tk.Label] = {}
        self._photo_refs: dict[str, ImageTk.PhotoImage] = {}
        self._closing = False
        self._thread: threading.Thread | None = None

    def start(self):
        """Launch the GUI (blocks until window is closed)."""
        self._build_and_run()

    def start_background(self):
        """Launch the GUI in a background thread (non-blocking)."""
        self._thread = threading.Thread(target=self._build_and_run, daemon=True,
                                        name="StreamGUI")
        self._thread.start()

    def stop(self):
        """Close the GUI window programmatically."""
        self._closing = True
        if self._root:
            self._root.after(0, self._root.destroy)

    def _build_and_run(self):
        root = tk.Tk()
        self._root = root
        root.title("Camera Stream Viewer")
        root.configure(bg="#1e1e1e")
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        cam_names = sorted(self.frame_handlers.keys())
        num_cams = max(len(cam_names), 1)
        win_w = num_cams * (DISPLAY_W + 20) + 10
        win_h = DISPLAY_H + 80
        sx = root.winfo_screenwidth() // 2 - win_w // 2
        sy = root.winfo_screenheight() // 2 - win_h // 2
        root.geometry(f"{win_w}x{win_h}+{sx}+{sy}")

        for col, cam_name in enumerate(cam_names):
            container = tk.Frame(root, bg="#1e1e1e")
            container.grid(row=0, column=col, padx=5, pady=5)

            tk.Label(
                container, text=cam_name, fg="white", bg="#1e1e1e",
                font=("Segoe UI", 11, "bold"),
            ).pack(pady=(0, 2))

            canvas = tk.Canvas(container, width=DISPLAY_W, height=DISPLAY_H,
                               bg="black", highlightthickness=0)
            canvas.pack()
            self._panels[cam_name] = canvas

            status = tk.Label(
                container, text="Waiting...", fg="orange", bg="#1e1e1e",
                font=("Segoe UI", 9),
            )
            status.pack(pady=(2, 0))
            self._status_labels[cam_name] = status

        self._update_frames()
        root.mainloop()

    def _update_frames(self):
        if self._closing or not self._root:
            return

        for cam_name, fh in self.frame_handlers.items():
            frame = fh.getFrame()
            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                img = img.resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
                photo = ImageTk.PhotoImage(image=img)
                self._panels[cam_name].create_image(0, 0, anchor=tk.NW, image=photo)
                self._photo_refs[cam_name] = photo
                self._status_labels[cam_name].configure(text="Live", fg="#00ff00")
            else:
                self._status_labels[cam_name].configure(text="No frame", fg="red")

        self._root.after(UPDATE_MS, self._update_frames)

    def _on_close(self):
        logger.info("Closing stream viewer ...")
        self._closing = True
        if self._root:
            self._root.destroy()

