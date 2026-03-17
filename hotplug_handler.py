import multiprocessing
import time
from multiprocessing import Queue
from typing import Any, Callable, Optional

from cds_py_logger import Logger
from cds_py_hotplug import USB_DEVICES, HOTPLUG


class CameraHotplugHandler(multiprocessing.Process):
    """
    Detects camera plug / unplug events using cds_py_hotplug.

    Mirrors the GenericHidReader pattern exactly:
      - Runs as a separate Process.
      - Registers a USB_DEVICES monitor for the camera's VID/PID.
      - HOTPLUG fires _on_connect / _on_disconnect callbacks automatically.
      - Puts {"isConnected": bool} messages onto connection_status queue.
      - Optionally calls user-supplied on_connect / on_disconnect callables.

    Args:
        config             : Dict with keys "vid", "pid", "device_name" (str).
        connection_status  : multiprocessing.Queue for status messages.
        on_connect         : Optional callable fired when camera is plugged in.
        on_disconnect      : Optional callable fired when camera is unplugged.
        logger             : cds_py_logger Logger instance.

    Example config for Logitech c922 Pro Stream Webcam:
        config = {
            "vid": "0x046d",
            "pid": "0x085c",
            "device_name": "c922 Pro Stream Webcam",
        }

    Usage:
        from multiprocessing import Queue
        status_q = Queue()
        handler = CameraHotplugHandler(
            config={"vid": "0x046d", "pid": "0x085c", "device_name": "c922"},
            connection_status=status_q,
            on_connect=lambda: print("Camera plugged in"),
            on_disconnect=lambda: print("Camera unplugged"),
        )
        handler.start()

        while True:
            msg = status_q.get()   # {"isConnected": True/False}
            print(msg)
    """

    def __init__(
        self,
        config: dict[str, Any],
        connection_status: Queue,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        logger: Logger = Logger(logger_name="CameraHotplugHandler"),
    ) -> None:
        super().__init__()

        self._logger = logger
        self._logger.info(
            f"[CameraHotplug] Init — device: {config.get('device_name', 'unknown')} "
            f"VID={config.get('vid')} PID={config.get('pid')}"
        )

        self.connection_status = connection_status
        self._on_connect_cb    = on_connect
        self._on_disconnect_cb = on_disconnect

        # Shared flag readable from parent process via .value
        self.is_connected = multiprocessing.Value("b", False)

        self._camera_device = USB_DEVICES(
            name=config.get("device_name", "Camera"),
            vendor_id=config["vid"],
            product_id=config["pid"],
            subsystem="video4linux",          # udev subsystem for USB video devices
            connection_cb=self._on_connect,
            disconnection_cb=self._on_disconnect,
            logger_object=self._logger,
        )

        self._hotplug = HOTPLUG(logger_object=self._logger)
        self._hotplug.register_device(self._camera_device)

        # Check if camera is already connected at init time
        self._probe_initial_state()

    # ── Process entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """Start udev monitoring loop — blocks forever (daemon process)."""
        self._logger.info("[CameraHotplug] Hotplug monitor started")
        self._hotplug.start()          # blocks; udev events call _on_connect / _on_disconnect

    # ── udev callbacks ─────────────────────────────────────────────────────────

    def _on_connect(self) -> None:
        """Called by HOTPLUG when the camera is plugged in."""
        self._logger.info("[CameraHotplug] Camera connected")
        self.is_connected.value = True
        self.connection_status.put({"isConnected": True}, block=False)
        if self._on_connect_cb:
            try:
                self._on_connect_cb()
            except Exception as exc:
                self._logger.error(f"[CameraHotplug] on_connect callback error: {exc}")

    def _on_disconnect(self) -> None:
        """Called by HOTPLUG when the camera is unplugged."""
        self._logger.warning("[CameraHotplug] Camera disconnected")
        self.is_connected.value = False
        self.connection_status.put({"isConnected": False}, block=False)
        if self._on_disconnect_cb:
            try:
                self._on_disconnect_cb()
            except Exception as exc:
                self._logger.error(f"[CameraHotplug] on_disconnect callback error: {exc}")

    # ── helpers ────────────────────────────────────────────────────────────────

    def _probe_initial_state(self) -> None:
        """Check whether the device is already present before monitoring starts."""
        try:
            path = self._camera_device.get_device_path()
            if path is not None:
                self._logger.info(f"[CameraHotplug] Camera already present at: {path}")
                self.is_connected.value = True
                self.connection_status.put({"isConnected": True}, block=False)
            else:
                self._logger.info("[CameraHotplug] Camera not detected at startup")
        except Exception as exc:
            self._logger.warning(f"[CameraHotplug] Could not probe initial state: {exc}")


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from multiprocessing import Queue as MPQueue

    status_queue: MPQueue = MPQueue()

    # Logitech c922 Pro Stream Webcam VID/PID — adjust if your camera differs
    camera_config = {
        "vid": "0x046d",
        "pid": "0x085c",
        "device_name": "c922 Pro Stream Webcam",
    }

    handler = CameraHotplugHandler(
        config=camera_config,
        connection_status=status_queue,
        on_connect=lambda: print("[Test] >>> Camera plugged in"),
        on_disconnect=lambda: print("[Test] >>> Camera unplugged"),
    )
    handler.daemon = True
    handler.start()

    print("[Test] Monitoring camera hotplug events — plug/unplug your camera...")
    try:
        while True:
            msg = status_queue.get()
            print(f"[Test] Status update: {msg}")
    except KeyboardInterrupt:
        print("[Test] Stopped")
        handler.terminate()
        handler.join(timeout=3)
