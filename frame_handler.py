import enum
import logging
import queue as _queue_module
import threading
from typing import Optional, Dict, Tuple, List
from flask_socketio import SocketIO
import cv2
import datetime
import ffmpegcv
import math
import numpy as _np
import pytz
import time


class StreamState(enum.Enum):
    """Observable lifecycle state of a single stream slot inside FrameHandlerWatchdog."""
    INITIALIZING  = "initializing"   # handler being created for the first time
    LIVE          = "live"           # frames flowing normally
    CRASHED       = "crashed"        # frames went stale — watchdog is restarting
    DISCONNECTED  = "disconnected"   # physical unplug detected — not retrying
    RECONNECTING  = "reconnecting"   # plug-in detected — creating new handler

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
            ):
        """
        cameraServerLink should be the MJPEG stream URL (not the HTML page).
        Example: http://localhost:1984/api/stream.mjpeg?src=inspect

        max_retries: Number of open attempts before raising RuntimeError.
            Use the default (5) for direct/standalone construction.
            The watchdog passes 1 so it fails fast and relies on its own
            check_interval for retry pacing.
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

        self._is_http = self.camera_server_link.startswith(("http://", "https://"))

        if self._is_http:
            # HTTP/MJPEG: go2rtc holds the HTTP connection open even with no
            # active publisher.  We use requests streaming to read frames
            # directly — no cv2.VideoCapture needed at init time and no
            # FFmpeg async_lock concerns.
            self.cap = None
        else:
            # RTSP / other protocols: use cv2.VideoCapture with FFmpeg backend.
            # CAP_PROP_OPEN_TIMEOUT_MSEC limits the initial TCP/RTSP handshake.
            # CAP_PROP_READ_TIMEOUT_MSEC makes cap.read() return False within
            # 5 s on a dead stream so the reader thread exits cleanly.
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
                    # Transient read failure (timeout on dead stream, etc.).
                    # Small sleep avoids a tight spin loop before the next attempt.
                    time.sleep(0.05)
        finally:
            # Release from the owning thread — safe, no async_lock race.
            try:
                self.cap.release()
            except Exception:
                pass

    def _reader_http(self) -> None:
        """
        cv2.VideoCapture reader for HTTP/MJPEG streams.

        Runs entirely inside the reader thread so __init__ always succeeds
        even when no publisher is active.  The open attempt is retried every
        2 s until go2rtc has an active producer — once opened, frames flow
        via the same cap.read() path as the RTSP reader.

        cap is a local variable — released in the finally block of the inner
        try, so there is no async_lock race with any other thread.
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
                    time.sleep(2)   # no publisher yet — wait and retry
                continue
            # Capture opened — stream frames until it stops delivering
            try:
                while self.running:
                    ret, frame = cap.read()
                    if ret:
                        with self.frame_lock:
                            self.latest_frame = frame
                            self.is_latest_frame_available = True
                            self.last_received_time = time.monotonic()
                    elif self.running:
                        # Read failure — stream may have dropped; re-open.
                        time.sleep(0.05)
                        break
            finally:
                try:
                    cap.release()
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

        For RTSP streams: cap.release() is called by the reader thread itself
        (in its finally block) — never from here — to eliminate the async_lock
        race.  With CAP_PROP_READ_TIMEOUT_MSEC=5000 the blocking cap.read()
        returns within 5 s, so the join completes quickly.

        For HTTP/MJPEG streams: the requests reader uses timeout=(5, 2), so
        iter_content() unblocks within 2 s of self.running being set False.
        self.cap is None for HTTP streams (no VideoCapture involved).
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

    def __init__(
        self,
        streams: Dict[str, str],
        stale_timeout: float = 10.0,
        check_interval: float = 3.0,
        on_restart=None,
        on_fail=None,
        hotplug_queues: Optional[Dict[str, _queue_module.Queue]] = None,
    ) -> None:
        """
        Args:
            streams        : {camera_name: url} — one entry per camera.
            stale_timeout  : Seconds without a new frame before declaring a crash (default 10).
            check_interval : Watchdog polling period in seconds (default 3).
            on_restart     : Optional callable(camera_name) called after a successful restart.
            on_fail        : Optional callable(camera_name) called when a restart attempt fails.
            hotplug_queues : Optional {camera_name: queue.Queue} where each queue carries
                             {"isConnected": bool} messages from a CameraHotplugHandler.
                             On disconnect the handler is released immediately and the slot
                             is held in DISCONNECTED state (no retries) until a connect
                             message arrives.
        """
        self._streams        = dict(streams)
        self._stale_timeout  = stale_timeout
        self._check_interval = check_interval
        self._on_restart     = on_restart
        self._on_fail        = on_fail
        self._hotplug_queues: Dict[str, _queue_module.Queue] = dict(hotplug_queues) if hotplug_queues else {}

        self._handlers:        Dict[str, Optional["FrameHandler"]] = {}
        self._last_frame_time: Dict[str, float]                  = {}
        self._states:          Dict[str, StreamState]            = {}
        self._lock    = threading.Lock()
        self._running = True

        # Create initial handlers for all streams
        now = time.monotonic()
        for name, url in self._streams.items():
            self._states[name]          = StreamState.INITIALIZING
            handler                     = self._try_create(name, url)
            self._handlers[name]        = handler
            self._last_frame_time[name] = now
            if handler is not None:
                self._states[name] = StreamState.LIVE
            else:
                self._states[name] = StreamState.CRASHED

        self._thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="FrameHandlerWatchdog"
        )
        self._thread.start()
        _watchdog_logger.info("[Watchdog] Started monitoring %d stream(s)", len(self._streams))

    # ── public interface ───────────────────────────────────────────────────────

    def get_handler(self, name: str) -> Optional["FrameHandler"]:
        """Return the current live FrameHandler for *name*, or None if crashed."""
        with self._lock:
            return self._handlers.get(name)

    def get_frame(self, name: str):
        """Convenience wrapper: return the latest frame for *name*, or None."""
        fh = self.get_handler(name)
        return fh.getFrame() if fh is not None else None

    def camera_names(self) -> List[str]:
        """Return all monitored camera names."""
        return list(self._streams.keys())

    def get_state(self, name: str) -> StreamState:
        """Return the current StreamState for *name*."""
        with self._lock:
            return self._states.get(name, StreamState.INITIALIZING)

    def notify_disconnect(self, name: str) -> None:
        """
        Signal a physical unplug for *name* directly (no queue required).
        Immediately releases the FrameHandler and holds the slot in
        DISCONNECTED state — the watchdog will not retry until
        notify_connect() is called or a connect message arrives on the queue.
        Thread-safe.
        """
        _watchdog_logger.warning("[Watchdog] notify_disconnect called for %s", name)
        self._handle_hotplug_disconnect(name)

    def notify_connect(self, name: str) -> None:
        """
        Signal that the camera is physically plugged back in for *name*.
        Transitions the slot from DISCONNECTED → RECONNECTING so the next
        watchdog cycle will attempt to create a fresh FrameHandler.
        Thread-safe.
        """
        _watchdog_logger.info("[Watchdog] notify_connect called for %s", name)
        self._handle_hotplug_connect(name)

    def stop(self) -> None:
        """Stop the watchdog thread and release all FrameHandlers cleanly."""
        self._running = False
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
            self._states.clear()
        for fh in handlers:
            if fh is not None:
                try:
                    fh.release()
                except Exception:
                    pass
        _watchdog_logger.info("[Watchdog] Stopped")

    # ── internal helpers ───────────────────────────────────────────────────────

    def _try_create(self, name: str, url: str) -> Optional["FrameHandler"]:
        """Attempt to create a new FrameHandler; return None on failure.

        Passes max_retries=1 so the open attempt fails fast (≤ 1 s) and the
        watchdog's own check_interval governs retry pacing.  This avoids
        5 × 1 s of log spam on every cycle when the stream is not yet up.
        """
        try:
            fh = FrameHandler(cameraServerLink=url, max_retries=1)
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
            self._drain_hotplug_queues()
            for name, url in list(self._streams.items()):
                self._check_one(name, url)

    def _check_one(self, name: str, url: str) -> None:
        with self._lock:
            fh    = self._handlers.get(name)
            state = self._states.get(name, StreamState.INITIALIZING)

        now = time.monotonic()

        # ── DISCONNECTED: physical unplug — hold off until reconnect ────────
        # _drain_hotplug_queues() will transition this to RECONNECTING when
        # the connect message arrives; until then do nothing.
        if state == StreamState.DISCONNECTED:
            return

        # ── handler is None (crashed / reconnecting / never opened) ─────────
        if fh is None:
            new_fh = self._try_create(name, url)
            with self._lock:
                # Guard: a disconnect may have arrived while we were inside
                # _try_create (which can block up to 5 s × retries).
                if self._states.get(name) == StreamState.DISCONNECTED:
                    _watchdog_logger.warning(
                        "[Watchdog] %s disconnected during handler creation — discarding", name
                    )
                    discarded = new_fh
                    new_fh = None
                else:
                    discarded = None
                    self._handlers[name]        = new_fh
                    self._last_frame_time[name] = now
                    self._states[name]          = (
                        StreamState.LIVE if new_fh is not None else StreamState.CRASHED
                    )
            # Release outside the lock to avoid holding it during cap.release()
            if discarded is not None:
                try:
                    discarded.release()
                except Exception:
                    pass
                return
            if new_fh is not None:
                _watchdog_logger.info("[Watchdog] %s recovered after hotplug/crash", name)
                self._fire(self._on_restart, name)
            else:
                self._fire(self._on_fail, name)
            return

        # ── handler exists: is it still delivering NEW frames? ──────────────
        # We use last_received_time (updated by the reader thread on every
        # successful cap.read()) rather than getFrame() which returns the
        # last cached frame even when the stream is dead.
        age = now - fh.last_received_time
        if age < self._stale_timeout:
            with self._lock:
                self._last_frame_time[name] = fh.last_received_time
                if self._states.get(name) not in (StreamState.DISCONNECTED,):
                    self._states[name] = StreamState.LIVE
            return

        # ── stream crash detected — release old handler and recreate ─────────
        _watchdog_logger.warning(
            "[Watchdog] %s stale for %.1fs (threshold %.1fs) — restarting",
            name, age, self._stale_timeout,
        )
        with self._lock:
            if self._states.get(name) not in (StreamState.DISCONNECTED,):
                self._states[name] = StreamState.CRASHED
        try:
            fh.release()
        except Exception:
            pass

        new_fh = self._try_create(name, url)
        with self._lock:
            if self._states.get(name) == StreamState.DISCONNECTED:
                discarded = new_fh
                new_fh = None
            else:
                discarded = None
                self._handlers[name]        = new_fh
                self._last_frame_time[name] = now
                self._states[name]          = (
                    StreamState.LIVE if new_fh is not None else StreamState.CRASHED
                )
        if discarded is not None:
            try:
                discarded.release()
            except Exception:
                pass
            return
        if new_fh is not None:
            _watchdog_logger.info("[Watchdog] %s restarted successfully", name)
            self._fire(self._on_restart, name)
        else:
            _watchdog_logger.error("[Watchdog] %s restart failed — will retry next cycle", name)
            self._fire(self._on_fail, name)

    def _drain_hotplug_queues(self) -> None:
        """Process all pending hotplug messages without blocking."""
        for name, q in self._hotplug_queues.items():
            while True:
                try:
                    msg = q.get_nowait()
                    if not msg.get("isConnected", True):
                        self._handle_hotplug_disconnect(name)
                    else:
                        self._handle_hotplug_connect(name)
                except _queue_module.Empty:
                    break

    def _handle_hotplug_disconnect(self, name: str) -> None:
        """
        Immediately release the live FrameHandler and set the slot to
        DISCONNECTED.  The watchdog will not attempt to recreate the handler
        until a connect event arrives via the queue or notify_connect().
        """
        with self._lock:
            fh = self._handlers.get(name)
            self._handlers[name] = None
            self._states[name]   = StreamState.DISCONNECTED
        if fh is not None:
            _watchdog_logger.warning(
                "[Watchdog] %s DISCONNECTED — releasing FrameHandler", name
            )
            try:
                fh.release()
            except Exception:
                pass
        self._fire(self._on_fail, name)

    def _handle_hotplug_connect(self, name: str) -> None:
        """
        Transition the slot from DISCONNECTED → RECONNECTING so the next
        _watch_loop cycle will call _try_create for this camera.
        If the slot is not DISCONNECTED the event is a spurious duplicate
        and is silently ignored.
        """
        with self._lock:
            if self._states.get(name) == StreamState.DISCONNECTED:
                self._states[name] = StreamState.RECONNECTING
                _watchdog_logger.info(
                    "[Watchdog] %s CONNECT received — will create handler on next cycle", name
                )

    @staticmethod
    def _fire(cb, name: str) -> None:
        if cb is None:
            return
        try:
            cb(name)
        except Exception as exc:
            _watchdog_logger.debug("[Watchdog] Callback error: %s", exc)