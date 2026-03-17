"""
main.py — Integration test: CameraStreamManager + FrameHandler + HID (optional).

Starts all components wired together:
  1. CameraStreamManager  — manages ffmpeg, hotplug, watchdog
  2. FrameHandler (x2)    — reads MJPEG from go2rtc, auto-restarts on crash
  3. GenericHidReader      — (Linux only) reads barcode, captures frame on scan

Run:
    python main.py
"""

import logging
import os
import sys
import time

from camera_stream_manager import CameraStreamManager, CameraConfig
from frame_handler import FrameHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────

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

# HID barcode scanner config (Linux only — ignored on Windows)
HID_CONFIG = {
    "vid": "0x05e0",
    "pid": "0x1200",
}


def main():
    # ── 1. Start CameraStreamManager ────────────────────────────────────
    logger.info("=== Starting CameraStreamManager ===")
    manager = CameraStreamManager(
        cameras=CAMERAS,
        go2rtc_api=GO2RTC_API,
        rtsp_server=RTSP_SERVER,
        on_camera_connected=lambda name: logger.info(f"[Main] Camera connected: {name}"),
        on_camera_disconnected=lambda name: logger.warning(f"[Main] Camera disconnected: {name}"),
    )
    manager.start()

    # Wait for streams to become healthy before creating FrameHandlers
    logger.info("Waiting for streams to become healthy...")
    for attempt in range(20):
        all_healthy = all(
            manager.is_stream_healthy(name)
            for name, state in ((n, manager.get_state(n)) for n in CAMERAS)
            if state and state.is_connected
        )
        if all_healthy:
            logger.info("All connected streams are healthy")
            break
        time.sleep(2)
    else:
        logger.warning("Not all streams became healthy — creating FrameHandlers anyway")

    # ── 2. Create FrameHandlers ─────────────────────────────────────────
    frame_handlers: dict[str, FrameHandler] = {}
    for cam_name in CAMERAS:
        rtsp_url = manager.get_rtsp_url(cam_name)
        state = manager.get_state(cam_name)
        if state and state.is_connected:
            try:
                fh = FrameHandler(
                    cameraServerLink=rtsp_url,
                    streamManager=manager,
                    cameraName=cam_name,
                )
                frame_handlers[cam_name] = fh
                logger.info(f"[Main] FrameHandler created for {cam_name} -> {rtsp_url}")
            except Exception as e:
                logger.error(f"[Main] Failed to create FrameHandler for {cam_name}: {e}")
        else:
            logger.warning(f"[Main] {cam_name} not connected — skipping FrameHandler")

    # ── 3. Start HID reader (Linux only) ────────────────────────────────
    hid_reader = None
    if sys.platform == "linux":
        try:
            from hid_handler import GenericHidReader
            from multiprocessing import Queue as MPQueue

            hid_data_queue: MPQueue = MPQueue()
            hid_status_queue: MPQueue = MPQueue()
            hid_reader = GenericHidReader(
                data_queue=hid_data_queue,
                config=HID_CONFIG,
                connection_status=hid_status_queue,
                frame_handlers=frame_handlers,
            )
            hid_reader.start()
            logger.info("[Main] HID reader started")
        except ImportError as e:
            logger.warning(f"[Main] HID handler not available (missing deps): {e}")
        except Exception as e:
            logger.error(f"[Main] Failed to start HID reader: {e}")
    else:
        logger.info("[Main] HID reader skipped (Windows — evdev is Linux only)")

    # ── 4. Run: periodic frame grab test + keep alive ───────────────────
    logger.info("=== All components running — press Ctrl+C to stop ===")
    os.makedirs("test_frames", exist_ok=True)

    try:
        cycle = 0
        while True:
            time.sleep(5)
            cycle += 1

            # Every 10 cycles (50s), grab and save a test frame from each camera
            if cycle % 10 == 1:
                for cam_name, fh in frame_handlers.items():
                    frame = fh.getFrame()
                    if frame is not None:
                        path = f"test_frames/{cam_name}_latest.jpg"
                        fh.getAndSaveFrame(path)
                        logger.info(f"[Main] Saved test frame: {path} (shape={frame.shape})")
                    else:
                        logger.warning(f"[Main] {cam_name} returned no frame")

            # Print status summary
            status_parts = []
            for cam_name in CAMERAS:
                state = manager.get_state(cam_name)
                if state:
                    h = "HEALTHY" if state.healthy else "UNHEALTHY"
                    conn = "connected" if state.is_connected else "disconnected"
                    pid = state.process.pid if state.process and state.process.poll() is None else "N/A"
                    fh = frame_handlers.get(cam_name)
                    has_frame = fh is not None and fh.getFrame() is not None
                    status_parts.append(
                        f"{cam_name}: {h} ({conn}, PID={pid}, frame={'YES' if has_frame else 'NO'})"
                    )
            logger.info(f"[Status] {' | '.join(status_parts)}")

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        # Clean up
        for cam_name, fh in frame_handlers.items():
            logger.info(f"[Main] Releasing FrameHandler for {cam_name}")
            fh.release()
        manager.stop()
        if hid_reader and hid_reader.is_alive():
            hid_reader.terminate()
            hid_reader.join(timeout=5)
        logger.info("=== Shutdown complete ===")


if __name__ == "__main__":
    main()
