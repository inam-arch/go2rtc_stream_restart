import logging
import threading
from flask_socketio import SocketIO
import cv2
import datetime
import ffmpegcv
import math
import pytz
import time

from camera_stream_manager import CameraStreamManager

logger = logging.getLogger(__name__)

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
            videoResolution: tuple[int, int] = (640, 540),
            streamManager: CameraStreamManager | None = None,
            cameraName: str = "",
            maxReadFailures: int = 30,
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

        # Stream management integration
        self.stream_manager = streamManager
        self.camera_name = cameraName
        self.max_read_failures = maxReadFailures
        self._consecutive_failures = 0
        self._restart_in_progress = False

        # If a stream manager is provided, ensure the stream is running first
        if self.stream_manager and self.camera_name:
            self._ensure_stream_running()

        # Threaded MJPEG capture with retry mechanism
        self.cap = self._open_capture()

        self.latest_frame = None
        self.is_latest_frame_available = False
        self.frame_lock = threading.Lock()
        self.running = True
        self.reader_thread = threading.Thread(target=self._reader, daemon=True, name=f"MJPEGReader-{self.camera_name or 'default'}")
        self.reader_thread.start()

    def _open_capture(self) -> cv2.VideoCapture:
        """Open the MJPEG stream with retries. If a stream manager is present
        and the stream is not running, start it before connecting."""
        max_retries = 10
        retry_delay = 2
        for attempt in range(1, max_retries + 1):
            cap = cv2.VideoCapture(self.camera_server_link)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if cap.isOpened():
                return cap
            cap.release()

            if attempt < max_retries:
                logger.warning(
                    f"[FrameHandler] Attempt {attempt}/{max_retries} failed to open MJPEG stream. "
                    f"Retrying in {retry_delay}s..."
                )
                # If stream manager available, try starting the stream on first failure
                if self.stream_manager and self.camera_name and attempt == 1:
                    if not self.stream_manager.is_stream_running(self.camera_name):
                        logger.info(f"[FrameHandler] Requesting stream start for {self.camera_name}")
                        self.stream_manager.start_stream(self.camera_name)
                time.sleep(retry_delay)
            else:
                raise RuntimeError(
                    f"Failed to open MJPEG stream at {self.camera_server_link} "
                    f"after {max_retries} attempts."
                )
        # Should not reach here, but satisfy type checker
        raise RuntimeError("Unexpected: all retries exhausted")

    def _ensure_stream_running(self):
        """Wait for the camera stream to become healthy before connecting.
        If it's not running, ask the stream manager to start it."""
        if not self.stream_manager or not self.camera_name:
            return

        if self.stream_manager.is_stream_healthy(self.camera_name):
            return

        logger.info(f"[FrameHandler] Stream {self.camera_name} not healthy — requesting start")
        if not self.stream_manager.is_stream_running(self.camera_name):
            self.stream_manager.start_stream(self.camera_name)

        # Wait for the stream to become healthy (up to 30 s)
        for _ in range(30):
            if self.stream_manager.is_stream_healthy(self.camera_name):
                logger.info(f"[FrameHandler] Stream {self.camera_name} is now healthy")
                return
            time.sleep(1)
        logger.warning(f"[FrameHandler] Stream {self.camera_name} did not become healthy in time")

    def _reader(self):
        """Continuously grab frames and keep only the latest one.
        Detects consecutive read failures and triggers a per-stream restart."""
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                self._consecutive_failures = 0
                with self.frame_lock:
                    self.latest_frame = frame
                    self.is_latest_frame_available = True
            else:
                self._consecutive_failures += 1
                if (
                    self._consecutive_failures >= self.max_read_failures
                    and self.stream_manager
                    and self.camera_name
                    and not self._restart_in_progress
                ):
                    self._handle_stream_crash()
                time.sleep(0.1)  # Avoid busy-spin on failures

    def _handle_stream_crash(self):
        """Restart the crashed stream and reconnect the MJPEG capture.
        Only affects this camera's stream — other streams are untouched."""
        self._restart_in_progress = True
        try:
            logger.warning(
                f"[FrameHandler] Stream crash detected for {self.camera_name} "
                f"({self._consecutive_failures} consecutive read failures) — restarting"
            )
            self.stream_manager.restart_stream(self.camera_name)
            time.sleep(3)  # Wait for ffmpeg to push frames to go2rtc
            self._reconnect()
            self._consecutive_failures = 0
        except Exception as e:
            logger.error(f"[FrameHandler] Error handling stream crash for {self.camera_name}: {e}")
        finally:
            self._restart_in_progress = False

    def _reconnect(self):
        """Release the current capture and open a new one to the MJPEG stream."""
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass

        max_retries = 10
        for attempt in range(1, max_retries + 1):
            cap = cv2.VideoCapture(self.camera_server_link)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if cap.isOpened():
                self.cap = cap
                logger.info(f"[FrameHandler] Reconnected to {self.camera_name}")
                return
            cap.release()
            logger.info(f"[FrameHandler] Reconnect attempt {attempt}/{max_retries} for {self.camera_name}")
            time.sleep(2)

        logger.error(f"[FrameHandler] Failed to reconnect to {self.camera_name} after {max_retries} attempts")

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
        """Cleanly stop the reader thread and release resources"""
        self.running = False
        if self.reader_thread.is_alive():
            self.reader_thread.join()
        self.cap.release()

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