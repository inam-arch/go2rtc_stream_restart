import logging
import multiprocessing as _mp
import queue as _queue_module
import threading
from typing import Any, Optional, Dict, Tuple, List
from flask_socketio import SocketIO
import cv2
import datetime
import ffmpegcv
import math
import numpy as _np
import pytz
import time
from cds_py_logger import Logger
from cds_py_hotplug import USB_DEVICES, HOTPLUG


_logger = logging.getLogger("FrameHandler")


class _CameraHotplugProcess(_mp.Process):
    """Internal hotplug monitor process — created by FrameHandler when hotplug_config is provided.

    Runs as a daemon process.  Puts {"isConnected": bool} messages onto the
    supplied multiprocessing.Queue so the FrameHandler watchdog can react.

    Args:
        config            : {"vid": str, "pid": str, "device_name": str}
        connection_status : multiprocessing.Queue for status messages.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        connection_status: _mp.Queue,
    ) -> None:
        super().__init__(daemon=True)
        self._config = config
        self.connection_status = connection_status
        self.is_connected = _mp.Value("b", False)
        # Defer heavy setup to run() so it happens inside the child process.

    def run(self) -> None:
        logger = Logger(logger_name="CameraHotplug")
        camera_device = USB_DEVICES(
            name=self._config.get("device_name", "Camera"),
            vendor_id=self._config["vid"],
            product_id=self._config["pid"],
            subsystem="video4linux",
            connection_cb=self._on_connect,
            disconnection_cb=self._on_disconnect,
            logger_object=logger,
        )
        hotplug = HOTPLUG(logger_object=logger)
        hotplug.register_device(camera_device)

        # Probe initial state
        try:
            path = camera_device.get_device_path()
            if path is not None:
                self.is_connected.value = True
                self.connection_status.put({"isConnected": True}, block=False)
        except Exception:
            pass

        logger.info("[CameraHotplug] Hotplug monitor started")
        hotplug.start()  # blocks forever; udev events fire callbacks

    def _on_connect(self) -> None:
        self.is_connected.value = True
        self.connection_status.put({"isConnected": True}, block=False)

    def _on_disconnect(self) -> None:
        self.is_connected.value = False
        self.connection_status.put({"isConnected": False}, block=False)


class FrameHandler:
    LATEST_FRAME_TIMEOUT = 1 # seconds

    def __init__(
            self,
            flaskSocketIo: Optional[SocketIO] = None,
            channelName: str = "frame",
            roomId: int = 122,
            cameraServerLink: str = "http://127.0.0.1:1984/api/stream.mjpeg?src=inspect",
            saveVideo: bool = False,
            enableResize: bool = False,
            videoFPS: int = 30,
            imageResolution: Tuple[int, int] = (2464, 2056),
            videoResolution: Tuple[int, int] = (640, 540),
            max_retries: int = 5,
            stale_timeout: float = 10.0,
            check_interval: float = 3.0,
            on_restart=None,
            on_fail=None,
            hotplug_queue: Optional[_queue_module.Queue] = None,
            hotplug_config: Optional[Dict[str, str]] = None,
            enable_watchdog: bool = True,
            name: str = "",
            ):
        """
        cameraServerLink should be the MJPEG stream URL (not the HTML page).
        Example: http://localhost:1984/api/stream.mjpeg?src=inspect

        max_retries: Number of open attempts before raising RuntimeError.
            Use the default (5) for direct/standalone construction.

        Watchdog params (all optional — backward compatible):
            stale_timeout   : Seconds without a new frame before restarting reader (default 10).
            check_interval  : Watchdog polling period in seconds (default 3).
            on_restart      : Optional callable(name) called after a successful restart.
            on_fail         : Optional callable(name) called when a restart fails.
            hotplug_queue   : Optional queue carrying {"isConnected": bool} messages.
            hotplug_config  : Optional {"vid": str, "pid": str, "device_name": str}.
                              When provided, an internal hotplug monitor process is
                              started automatically (requires cds_py_hotplug on Linux).
                              Ignored if hotplug_queue is already supplied.
            enable_watchdog : Set False to disable automatic restart (default True).
            name            : Optional identifier for log messages and callbacks.
        """
        self.flaskSocketIo = flaskSocketIo
        self.channelName = channelName
        self.room_id = roomId
        self.camera_server_link = cameraServerLink
        self.save_video = saveVideo
        self.enableResize = enableResize
        self.videoFPS = videoFPS
        self.imageResolution = imageResolution
        self.imageWidth = self.imageResolution[0]
        self.imageHeight = self.imageResolution[1]
        self.videoResolution = videoResolution
        self.videoWidth = self.videoResolution[0]
        self.videoHeight = self.videoResolution[1]
        self.save_in_progress = False
        self.last_frame = None
        self.last_timestamp = None
        self.videoSavingLoop: Optional[threading.Thread] = None
        self.name = name

        self._is_http = self.camera_server_link.startswith(("http://", "https://"))

        if self._is_http:
            self.cap = None
        else:
            _cap_params = [
                int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), 5_000,
                int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), 5_000,
            ]
            retry_delay = 1
            for attempt in range(1, max_retries + 1):
                self.cap = cv2.VideoCapture(self.camera_server_link, cv2.CAP_FFMPEG, _cap_params)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if self.cap.isOpened():
                    break
                else:
                    self.cap.release()
                    if attempt < max_retries:
                        print(f"[FrameHandler] Attempt {attempt} failed to open stream. Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                    else:
                        raise RuntimeError(f"Failed to open stream at {self.camera_server_link} after {max_retries} attempts.")

        self.latest_frame = None
        self.is_latest_frame_available = False
        self.last_received_time: float = time.monotonic()
        self.frame_lock = threading.Lock()
        self.running = True
        _target = self._reader_http if self._is_http else self._reader
        self.reader_thread = threading.Thread(target=_target, daemon=True, name="MJPEGReader")
        self.reader_thread.start()

        # ── Watchdog ──────────────────────────────────────────────────────
        self._stale_timeout = stale_timeout
        self._check_interval = check_interval
        self._on_restart = on_restart
        self._on_fail = on_fail
        self._hotplug_queue = hotplug_queue
        self._hotplug_process: Optional[_CameraHotplugProcess] = None

        # Auto-create hotplug monitor when config is provided and no external
        # queue was passed in.
        if hotplug_config is not None and hotplug_queue is None:
            hp_queue: _mp.Queue = _mp.Queue()
            self._hotplug_queue = hp_queue
            self._hotplug_process = _CameraHotplugProcess(
                config=hotplug_config,
                connection_status=hp_queue,
            )
            self._hotplug_process.start()
            _logger.info("[FrameHandler] Hotplug monitor started for %s", self.name or self.camera_server_link)

        self._disconnected = False
        self._wd_running = enable_watchdog
        self._wd_thread: Optional[threading.Thread] = None
        if enable_watchdog:
            self._wd_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="WatchdogThread"
            )
            self._wd_thread.start()

    # ── Reader threads ─────────────────────────────────────────────────────

    def _reader(self):
        """
        cv2.VideoCapture reader for RTSP / non-HTTP streams.
        Owns and releases self.cap — cap.release() is NEVER called from
        outside this thread to avoid the FFmpeg async_lock race condition.
        """
        try:
            while self.running:
                ret, frame = self.cap.read()
                if ret:
                    with self.frame_lock:
                        self.latest_frame = frame
                        self.is_latest_frame_available = True
                        self.last_received_time = time.monotonic()
                elif self.running:
                    time.sleep(0.05)
        finally:
            try:
                self.cap.release()
            except Exception:
                pass

    def _reader_http(self) -> None:
        """
        cv2.VideoCapture reader for HTTP/MJPEG streams.
        Runs entirely inside the reader thread so __init__ always succeeds
        even when no publisher is active.
        """
        _cap_params = [
            int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), 5_000,
            int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), 5_000,
        ]
        while self.running:
            cap = cv2.VideoCapture(self.camera_server_link, cv2.CAP_FFMPEG, _cap_params)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                cap.release()
                if self.running:
                    time.sleep(2)
                continue
            try:
                while self.running:
                    ret, frame = cap.read()
                    if ret:
                        with self.frame_lock:
                            self.latest_frame = frame
                            self.is_latest_frame_available = True
                            self.last_received_time = time.monotonic()
                    elif self.running:
                        time.sleep(0.05)
                        break
            finally:
                try:
                    cap.release()
                except Exception:
                    pass

    # ── Built-in watchdog ──────────────────────────────────────────────────

    def _restart_reader(self) -> bool:
        """Stop the current reader thread and start a fresh one.
        Returns True on success, False on failure or if disconnected."""
        self.running = False
        if self.reader_thread.is_alive():
            self.reader_thread.join(timeout=8)

        if self._disconnected:
            return False

        if self._is_http:
            self.running = True
            self.reader_thread = threading.Thread(
                target=self._reader_http, daemon=True, name="MJPEGReader"
            )
            self.reader_thread.start()
            return True
        else:
            _cap_params = [
                int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), 5_000,
                int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), 5_000,
            ]
            self.cap = cv2.VideoCapture(self.camera_server_link, cv2.CAP_FFMPEG, _cap_params)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not self.cap.isOpened():
                self.cap.release()
                return False
            self.running = True
            self.reader_thread = threading.Thread(
                target=self._reader, daemon=True, name="MJPEGReader"
            )
            self.reader_thread.start()
            return True

    def _watchdog_loop(self) -> None:
        tag = self.name or self.camera_server_link
        while self._wd_running:
            time.sleep(self._check_interval)
            if not self._wd_running:
                break

            # Drain hotplug queue (works with both queue.Queue and multiprocessing.Queue)
            if self._hotplug_queue is not None:
                while True:
                    try:
                        msg = self._hotplug_queue.get_nowait()
                        if not msg.get("isConnected", True):
                            self._handle_disconnect()
                        else:
                            self._handle_connect()
                    except Exception:
                        break

            if self._disconnected:
                continue

            age = time.monotonic() - self.last_received_time
            if age < self._stale_timeout:
                continue

            _logger.warning("[FrameHandler] %s stale for %.1fs — restarting reader", tag, age)
            if self._restart_reader():
                self.last_received_time = time.monotonic()
                _logger.info("[FrameHandler] %s reader restarted", tag)
                self._fire(self._on_restart)
            else:
                _logger.error("[FrameHandler] %s restart failed — retry next cycle", tag)
                self._fire(self._on_fail)

    def _handle_disconnect(self) -> None:
        tag = self.name or self.camera_server_link
        _logger.warning("[FrameHandler] %s DISCONNECTED", tag)
        self._disconnected = True
        self.running = False
        if self.reader_thread.is_alive():
            self.reader_thread.join(timeout=8)
        self._fire(self._on_fail)

    def _handle_connect(self) -> None:
        if not self._disconnected:
            return
        tag = self.name or self.camera_server_link
        _logger.info("[FrameHandler] %s CONNECT — will restart on next cycle", tag)
        self._disconnected = False

    # ── Public hotplug API ─────────────────────────────────────────────────

    def notify_disconnect(self) -> None:
        """Signal a physical unplug. Stops the reader immediately.
        The watchdog will not retry until notify_connect() is called."""
        self._handle_disconnect()

    def notify_connect(self) -> None:
        """Signal the camera is plugged back in.
        Reader restarts on the next watchdog cycle."""
        self._handle_connect()

    # ── Video saving ───────────────────────────────────────────────────────

    def savingLoop(self):
        try:
            with MJPEGStreamContextManager(self.camera_server_link) as mjpeg_stream:
                while self.save_in_progress:
                    frame = mjpeg_stream.getFrame()
                    time.sleep(1 / self.videoFPS)
                    if self.last_frame is not None and self.last_timestamp:
                        num_frames = math.floor(
                            (time.time() - self.last_timestamp) * self.videoFPS
                        )
                        for _ in range(num_frames - 1):
                            self._saveVideoFrame(self.last_frame)
                    self.last_frame = frame
                    self.last_timestamp = time.time()
                    self._saveVideoFrame(frame)
        except Exception as e:
            print(f"[FrameHandler] Error in savingLoop: {e}")
        finally:
            if hasattr(self, "out") and self.out is not None:
                try:
                    self.out.release()
                except Exception as e:
                    print(f"[FrameHandler] Error releasing writer: {e}")
                self.out = None
            self.last_timestamp = None
            print("[FrameHandler] Video saving loop ended and writer closed.")

    def setupVideoWriter(self, videoName: str = ""):
        if not videoName:
            timestamp: str = datetime.datetime.now(pytz.UTC).strftime("%y%m%d_%H%M%S")
            videoName = f"output_{timestamp}.mp4"
        encoder = "libx264"
        self.out = ffmpegcv.VideoWriter(  # type: ignore
            videoName, encoder, self.videoFPS, pix_fmt="bgr24", resize=(640, 480)
        )

    def startVideoSaving(self, videoName: str = ""):
        if self.save_in_progress:
            print("Video saving is already in progress.")
            return
        self.setupVideoWriter(videoName)
        self.save_in_progress = True
        self.last_frame = None
        self.videoSavingLoop = threading.Thread(
            target=self.savingLoop, daemon=True, name="VideoSavingThread"
        )
        self.videoSavingLoop.start()

    def stopVideoSaving(self):
        if not self.save_in_progress:
            print("No video saving in progress to stop.")
            return
        self.save_in_progress = False
        if self.videoSavingLoop and self.videoSavingLoop.is_alive():
            self.videoSavingLoop.join()
        self.videoSavingLoop = None

    def _saveVideoFrame(self, frame: cv2.typing.MatLike) -> None:
        if self.enableResize:
            resizedImage = cv2.resize(frame, self.videoResolution)
            self.out.write(resizedImage) # type: ignore
            return
        self.out.write(frame) # type: ignore

    # ── Frame access ───────────────────────────────────────────────────────

    def getAndSaveFrame(self, imagePath: str, wait_for_latest: bool = False) -> Optional[cv2.typing.MatLike]:
        frame = self.getFrame(wait_for_latest)
        if frame is not None:
            cv2.imwrite(imagePath, frame)
        return frame

    def check_camera_connection(self) -> bool:
        if self.getFrame() is None:
            attempt = 0
            while self.getFrame() is None:
                time.sleep(1)
                attempt += 1
                if attempt > 30:
                    return False
        return True

    def getFrame(self, wait_for_latest: bool = False) -> Optional[cv2.typing.MatLike]:
        frame = None
        if wait_for_latest:
            self.is_latest_frame_available = False
            while not self.is_latest_frame_available:
                time.sleep(0.001)
            with self.frame_lock:
                if self.latest_frame is not None:
                    frame = self.latest_frame.copy()
        else:
            with self.frame_lock:
                if self.latest_frame is not None:
                    frame = self.latest_frame.copy()
        return frame

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def release(self):
        """Stop reader, watchdog, and hotplug monitor."""
        self._wd_running = False
        self.running = False
        if self.reader_thread.is_alive():
            self.reader_thread.join(timeout=8)
        if self._wd_thread is not None and self._wd_thread.is_alive():
            self._wd_thread.join(timeout=self._check_interval + 2)
        if self._hotplug_process is not None and self._hotplug_process.is_alive():
            self._hotplug_process.terminate()
            self._hotplug_process.join(timeout=3)

    def _fire(self, cb) -> None:
        if cb is None:
            return
        try:
            cb(self.name)
        except Exception as exc:
            _logger.debug("[FrameHandler] Callback error: %s", exc)


class MJPEGStreamContextManager:
    def __init__(self, cameraServerLink: str):
        self.cameraServerLink = cameraServerLink
        self.running=True
        self.frame_lock=threading.Lock()
        self.latest_frame = None

    def __enter__(self) -> 'MJPEGStreamContextManager':
        self.video_save_source = cv2.VideoCapture(self.cameraServerLink)
        self.video_save_source.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.reader_thread = threading.Thread(target=self._reader)
        self.reader_thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.running = False
        self.reader_thread.join()
        self.video_save_source.release()
    
    def _reader(self):
        """ Continuously grab frames and keep only the latest one """
        while self.running:
            ret, frame = self.video_save_source.read()
            if ret:
                with self.frame_lock:
                    self.latest_frame = frame
    
    def getFrame(self) -> cv2.typing.MatLike:
        while self.latest_frame is None:
            time.sleep(0.001)
        with self.frame_lock:
            return self.latest_frame