"""
stream_screen.py — Tkinter GUI to display live camera streams.

Uses FrameHandler.getFrame() to grab frames and renders them in a window.
The GUI appears immediately; streams connect in the background.

Run:
    python stream_screen.py
"""

import logging
import threading
import time
import tkinter as tk

import cv2
from PIL import Image, ImageTk

from camera_stream_manager import CameraStreamManager, CameraConfig
from frame_handler import FrameHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

GO2RTC_API = "http://127.0.0.1:1984"
RTSP_SERVER = "rtsp://localhost:8554"

CAMERAS = {
    "camera1": CameraConfig(
        name="camera1",
        device_name="c922 Pro Stream Webcam",
        device_number=0,
    ),
    "camera2": CameraConfig(
        name="camera2",
        device_name="c922 Pro Stream Webcam",
        device_number=1,
    ),
}

DISPLAY_W = 320  # pixels per camera panel
DISPLAY_H = 240
UPDATE_MS = 33  # ~30 fps refresh rate


class StreamGUI:
    def __init__(self, root: tk.Tk, camera_names: list[str], manager: CameraStreamManager):
        self.root = root
        self.manager = manager
        self.camera_names = camera_names
        self.frame_handlers: dict[str, FrameHandler] = {}
        self.panels: dict[str, tk.Canvas] = {}
        self.status_labels: dict[str, tk.Label] = {}
        self._photo_refs: dict[str, ImageTk.PhotoImage] = {}
        self._closing = False

        self.root.title("Camera Stream Viewer")
        self.root.configure(bg="#1e1e1e")
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set window size: side-by-side panels + padding
        num_cams = len(camera_names)
        win_w = num_cams * (DISPLAY_W + 20) + 10
        win_h = DISPLAY_H + 80
        # Center on screen
        sx = self.root.winfo_screenwidth() // 2 - win_w // 2
        sy = self.root.winfo_screenheight() // 2 - win_h // 2
        self.root.geometry(f"{win_w}x{win_h}+{sx}+{sy}")

        # Build a column per camera immediately (shows "Waiting..." until ready)
        for col, cam_name in enumerate(sorted(camera_names)):
            container = tk.Frame(root, bg="#1e1e1e")
            container.grid(row=0, column=col, padx=5, pady=5)

            title = tk.Label(
                container, text=cam_name, fg="white", bg="#1e1e1e",
                font=("Segoe UI", 11, "bold"),
            )
            title.pack(pady=(0, 2))

            # Use a Canvas with fixed pixel size for the video panel
            canvas = tk.Canvas(container, width=DISPLAY_W, height=DISPLAY_H,
                               bg="black", highlightthickness=0)
            canvas.pack()
            self.panels[cam_name] = canvas

            status = tk.Label(
                container, text="Waiting for stream...", fg="orange", bg="#1e1e1e",
                font=("Segoe UI", 9),
            )
            status.pack(pady=(2, 0))
            self.status_labels[cam_name] = status

        # Start connecting to streams in a background thread
        threading.Thread(target=self._connect_streams, daemon=True).start()

        self._update_frames()

    def _connect_streams(self):
        """Wait for streams and create FrameHandlers in the background."""
        # Wait up to 40s for streams to become healthy
        for _ in range(20):
            if self._closing:
                return
            if all(self.manager.is_stream_healthy(n) for n in self.camera_names):
                break
            time.sleep(2)

        for cam_name in self.camera_names:
            if self._closing:
                return
            rtsp_url = self.manager.get_rtsp_url(cam_name)
            state = self.manager.get_state(cam_name)
            if state and state.is_connected:
                try:
                    fh = FrameHandler(
                        cameraServerLink=rtsp_url,
                        streamManager=self.manager,
                        cameraName=cam_name,
                    )
                    self.frame_handlers[cam_name] = fh
                    logger.info(f"FrameHandler ready: {cam_name} -> {rtsp_url}")
                except Exception as e:
                    logger.error(f"Failed to create FrameHandler for {cam_name}: {e}")
            else:
                logger.warning(f"{cam_name} not connected yet")

    def _update_frames(self):
        if self._closing:
            return

        for cam_name in self.camera_names:
            fh = self.frame_handlers.get(cam_name)
            if fh is None:
                # Not connected yet — keep showing waiting text
                state = self.manager.get_state(cam_name)
                if state and state.is_connected:
                    self.status_labels[cam_name].configure(
                        text="Connecting...", fg="orange",
                    )
                else:
                    self.status_labels[cam_name].configure(
                        text="Waiting for stream...", fg="orange",
                    )
                continue

            frame = fh.getFrame()
            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                img = img.resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
                photo = ImageTk.PhotoImage(image=img)
                canvas = self.panels[cam_name]
                canvas.create_image(0, 0, anchor=tk.NW, image=photo)
                self._photo_refs[cam_name] = photo
                self.status_labels[cam_name].configure(text="Live", fg="#00ff00")
            else:
                self.status_labels[cam_name].configure(text="No frame", fg="red")

        self.root.after(UPDATE_MS, self._update_frames)

    def _on_close(self):
        logger.info("Closing GUI ...")
        self._closing = True
        self.root.destroy()


def main():
    logger.info("Starting CameraStreamManager ...")
    manager = CameraStreamManager(
        cameras=CAMERAS,
        go2rtc_api=GO2RTC_API,
        rtsp_server=RTSP_SERVER,
    )
    manager.start()

    # Launch GUI immediately — streams connect in the background
    root = tk.Tk()
    gui = StreamGUI(root, list(CAMERAS.keys()), manager)
    logger.info("GUI open — close the window to stop")
    root.mainloop()

    # Cleanup after window closed
    logger.info("Shutting down ...")
    for fh in gui.frame_handlers.values():
        fh.release()
    manager.stop()
    logger.info("Done")


if __name__ == "__main__":
    main()
