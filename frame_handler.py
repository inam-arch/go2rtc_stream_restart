import logging
import threading
from flask_socketio import SocketIO
import cv2
import datetime
import ffmpegcv
import math
import pytz
import time

class FrameHandler:
    LATEST_FRAME_TIMEOUT = 1 # seconds

    def __init__(
            self,
            flaskSocketIo: SocketIO | None = None,
            channelName: str = "frame",
            roomId: int = 122,
            cameraServerLink: str = "http://127.0.0.1:1984/api/stream.mjpeg?src=inspect",
            saveVideo: bool = False,
            enableResize: bool = False,
            videoFPS: int = 30,
            imageResolution: tuple[int, int] = (2464, 2056),
            videoResolution: tuple[int, int] = (640, 540)
            ):
        """
        cameraServerLink should be the MJPEG stream URL (not the HTML page).
        Example: http://localhost:1984/api/stream.mjpeg?src=inspect
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
        self.videoSavingLoop: threading.Thread | None = None

        # CAP_PROP_READ_TIMEOUT_MSEC=5000 makes cap.read() return False within
        # 5 s on a dead/silent stream so the reader thread can exit cleanly.
        # On a live 30 fps stream each frame arrives in ~33 ms, so this timeout
        # never fires during normal operation.
        # CAP_PROP_OPEN_TIMEOUT_MSEC=5000 limits the initial connection attempt.
        # Both are passed as constructor params (apiPreference=CAP_FFMPEG).
        _cap_params = [
            int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), 5_000,
            int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), 5_000,
        ]
        max_retries = 5
        retry_delay = 1
        for attempt in range(1, max_retries + 1):
            self.cap = cv2.VideoCapture(self.camera_server_link, cv2.CAP_FFMPEG, _cap_params)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if self.cap.isOpened():
                break
            else:
                self.cap.release()
                if attempt < max_retries:
                    print(f"[FrameHandler] Attempt {attempt} failed to open MJPEG stream. Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    raise RuntimeError(f"Failed to open MJPEG stream at {self.camera_server_link} after {max_retries} attempts.")

        self.latest_frame = None
        self.is_latest_frame_available = False
        self.last_received_time: float = time.monotonic()
        self.frame_lock = threading.Lock()
        self.running = True
        self.reader_thread = threading.Thread(target=self._reader, daemon=True, name="MJPEGReader")
        self.reader_thread.start()

    def _reader(self):
        """
        Continuously grab frames and keep only the latest one.
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
                    # Transient read failure (timeout on dead stream, etc.).
                    # Small sleep avoids a tight spin loop before the next attempt.
                    time.sleep(0.05)
        finally:
            # Release from the owning thread — safe, no async_lock race.
            try:
                self.cap.release()
            except Exception:
                pass

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

    def getAndSaveFrame(self, imagePath: str, wait_for_latest: bool = False) -> cv2.typing.MatLike | None:
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

    def getFrame(self, wait_for_latest: bool = False) -> cv2.typing.MatLike | None:
        frame = None
        if wait_for_latest:
            # Block until we get a frame
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

    def release(self):
        """
        Signal the reader thread to stop and wait for it to exit.
        cap.release() is called by the reader thread itself (in its finally
        block) — never from here — to eliminate the async_lock race.
        With CAP_PROP_READ_TIMEOUT_MSEC=5000 the blocking cap.read() will
        return within 5 s, so the join completes quickly.
        """
        self.running = False
        if self.reader_thread.is_alive():
            self.reader_thread.join(timeout=8)

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


_watchdog_logger = logging.getLogger("FrameHandlerWatchdog")


class FrameHandlerWatchdog:
    """
    Monitors a pool of FrameHandlers and automatically restarts any that stop
    delivering frames (stream crash) or fail to open (hotplug disconnect).

    Does NOT modify FrameHandler — wraps it externally.

    Crash detection: if getFrame() returns None for longer than `stale_timeout`
    seconds, the handler is considered dead.  The watchdog calls release() on
    it, then constructs a fresh FrameHandler on the same URL.

    Hotplug detection: if FrameHandler.__init__ raises RuntimeError (camera not
    reachable), the slot is marked None and retried every `check_interval`
    seconds until the camera comes back.

    Args:
        streams        : {camera_name: url}  — one entry per camera.
        stale_timeout  : Seconds without a frame before declaring dead (default 10).
        check_interval : Watchdog polling period in seconds (default 3).
        on_restart     : Optional callable(camera_name) called after a successful restart.
        on_fail        : Optional callable(camera_name) called when a restart attempt fails.

    Example::

        watchdog = FrameHandlerWatchdog(
            streams={
                "camera1": "http://127.0.0.1:1984/api/stream.mjpeg?src=camera1",
                "camera2": "http://127.0.0.1:1984/api/stream.mjpeg?src=camera2",
            },
            stale_timeout=10,
            on_restart=lambda name: print(f"{name} restarted"),
        )
        frame = watchdog.get_frame("camera1")   # always returns latest or None
        watchdog.stop()                          # clean shutdown
    """

    def __init__(
        self,
        streams: dict[str, str],
        stale_timeout: float = 10.0,
        check_interval: float = 3.0,
        on_restart=None,
        on_fail=None,
    ) -> None:
        self._streams        = dict(streams)
        self._stale_timeout  = stale_timeout
        self._check_interval = check_interval
        self._on_restart     = on_restart
        self._on_fail        = on_fail

        self._handlers:        dict[str, "FrameHandler | None"] = {}
        self._last_frame_time: dict[str, float]                  = {}
        self._lock    = threading.Lock()
        self._running = True

        # Create initial handlers for all streams
        now = time.monotonic()
        for name, url in self._streams.items():
            self._handlers[name]        = self._try_create(name, url)
            self._last_frame_time[name] = now

        self._thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="FrameHandlerWatchdog"
        )
        self._thread.start()
        _watchdog_logger.info("[Watchdog] Started monitoring %d stream(s)", len(self._streams))

    # ── public interface ───────────────────────────────────────────────────────

    def get_handler(self, name: str) -> "FrameHandler | None":
        """Return the current live FrameHandler for *name*, or None if crashed."""
        with self._lock:
            return self._handlers.get(name)

    def get_frame(self, name: str):
        """Convenience wrapper: return the latest frame for *name*, or None."""
        fh = self.get_handler(name)
        return fh.getFrame() if fh is not None else None

    def camera_names(self) -> list[str]:
        """Return all monitored camera names."""
        return list(self._streams.keys())

    def stop(self) -> None:
        """Stop the watchdog thread and release all FrameHandlers cleanly."""
        self._running = False
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
        for fh in handlers:
            if fh is not None:
                try:
                    fh.release()
                except Exception:
                    pass
        _watchdog_logger.info("[Watchdog] Stopped")

    # ── internal helpers ───────────────────────────────────────────────────────

    def _try_create(self, name: str, url: str) -> "FrameHandler | None":
        """Attempt to create a new FrameHandler; return None on failure."""
        try:
            fh = FrameHandler(cameraServerLink=url)
            _watchdog_logger.info("[Watchdog] Handler created for %s", name)
            return fh
        except RuntimeError as exc:
            _watchdog_logger.warning("[Watchdog] Cannot open stream for %s: %s", name, exc)
            return None

    def _watch_loop(self) -> None:
        while self._running:
            time.sleep(self._check_interval)
            if not self._running:
                break
            for name, url in list(self._streams.items()):
                self._check_one(name, url)

    def _check_one(self, name: str, url: str) -> None:
        with self._lock:
            fh = self._handlers.get(name)

        now = time.monotonic()

        # ── handler is None: camera was dead, try to revive it ──────────────
        if fh is None:
            new_fh = self._try_create(name, url)
            with self._lock:
                self._handlers[name] = new_fh
                if new_fh is not None:
                    self._last_frame_time[name] = now
            if new_fh is not None:
                _watchdog_logger.info("[Watchdog] %s recovered after hotplug/crash", name)
                self._fire(self._on_restart, name)
            else:
                # Still failing — fire on_fail so the caller can restart ffmpeg/go2rtc.
                self._fire(self._on_fail, name)
            return

        # ── handler exists: is it still delivering NEW frames? ─────────────
        # getFrame() always returns the last cached frame (never None after the
        # first frame), so we check last_received_time instead.
        age = now - fh.last_received_time
        if age < self._stale_timeout:
            with self._lock:
                self._last_frame_time[name] = fh.last_received_time
            return

        # No new frame received within stale_timeout — measure staleness
        stale_for = age

        # ── stream crash detected — release old handler and recreate ─────────
        _watchdog_logger.warning(
            "[Watchdog] %s stale for %.1fs (threshold %.1fs) — restarting",
            name, stale_for, self._stale_timeout,
        )
        try:
            fh.release()
        except Exception:
            pass

        new_fh = self._try_create(name, url)
        with self._lock:
            self._handlers[name] = new_fh
            self._last_frame_time[name] = now

        if new_fh is not None:
            _watchdog_logger.info("[Watchdog] %s restarted successfully", name)
            self._fire(self._on_restart, name)
        else:
            _watchdog_logger.error("[Watchdog] %s restart failed — will retry next cycle", name)
            self._fire(self._on_fail, name)

    @staticmethod
    def _fire(cb, name: str) -> None:
        if cb is None:
            return
        try:
            cb(name)
        except Exception as exc:
            _watchdog_logger.debug("[Watchdog] Callback error: %s", exc)