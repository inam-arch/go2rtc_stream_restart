"""
CameraStreamManager — manages ffmpeg camera streams pushing RTSP to go2rtc.

Handles:
  - Starting/stopping individual camera ffmpeg processes
  - Hotplug detection (cameras connected/disconnected) via DirectShow device scanning
  - Stream health monitoring via go2rtc API
  - Auto-restart on stream crash without affecting other streams
  - Callbacks for camera connect/disconnect events

Usage:
    from camera_stream_manager import CameraStreamManager, CameraConfig

    cameras = {
        "camera1": CameraConfig(name="camera1", device_name="c922 Pro Stream Webcam", device_number=0),
        "camera2": CameraConfig(name="camera2", device_name="c922 Pro Stream Webcam", device_number=1),
    }
    manager = CameraStreamManager(cameras=cameras)
    manager.start()
"""

import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class CameraConfig:
    """Configuration for a single camera stream."""
    name: str
    device_name: str
    device_number: int
    video_size: str = "640x480"
    framerate: int = 30
    bitrate: str = "1000k"


@dataclass
class CameraState:
    """Runtime state for a single camera stream."""
    config: CameraConfig
    process: Optional[subprocess.Popen] = None
    log_file: object = field(default=None, repr=False)
    is_connected: bool = False
    restart_count: int = 0
    max_restarts: int = 5
    healthy: bool = False
    unhealthy_streak: int = 0
    healthy_streak: int = 0
    device_path: str = ""  # PNP path — unique per physical USB port
    lock: threading.Lock = field(default_factory=threading.Lock)
    cooldown_until: float = 0.0  # timestamp — watchdog skips this camera until then


class CameraStreamManager:
    """
    Manages camera ffmpeg processes that push RTSP to go2rtc Docker container.

    Features:
      - Start/stop/restart individual camera streams (independent of each other)
      - Detect camera hotplug via periodic DirectShow device enumeration
      - Monitor go2rtc API for producer health (watchdog)
      - Auto-restart crashed streams without affecting other streams
    """

    def __init__(
        self,
        cameras: dict[str, CameraConfig],
        go2rtc_api: str = "http://127.0.0.1:1984",
        rtsp_server: str = "rtsp://localhost:8554",
        health_check_interval: int = 5,
        hotplug_scan_interval: int = 5,
        max_restarts: int = 5,
        unhealthy_checks_before_restart: int = 2,
        healthy_checks_before_reset: int = 5,
        on_camera_connected: Optional[Callable[[str], None]] = None,
        on_camera_disconnected: Optional[Callable[[str], None]] = None,
    ):
        self.cameras = cameras
        self.go2rtc_api = go2rtc_api
        self.rtsp_server = rtsp_server
        self.health_check_interval = health_check_interval
        self.hotplug_scan_interval = hotplug_scan_interval
        self.max_restarts = max_restarts
        self.unhealthy_checks_before_restart = unhealthy_checks_before_restart
        self.healthy_checks_before_reset = healthy_checks_before_reset
        self.on_camera_connected = on_camera_connected
        self.on_camera_disconnected = on_camera_disconnected

        self._states: dict[str, CameraState] = {}
        for name, config in cameras.items():
            self._states[name] = CameraState(config=config, max_restarts=max_restarts)

        self._running = False
        self._watchdog_thread: Optional[threading.Thread] = None
        self._hotplug_thread: Optional[threading.Thread] = None

    # ── Public API ──────────────────────────────────────────────────────

    def start(self):
        """Start the manager: detect devices, start streams, begin monitoring."""
        self._running = True

        # Wait for go2rtc API to be reachable
        if not self._wait_for_api(timeout=30):
            logger.error("[StreamManager] go2rtc API not reachable — is Docker running?")
            return

        # Initial device scan — discover PNP paths for each physical camera
        device_map = self._scan_connected_devices()
        logger.info(f"[StreamManager] Initial device scan: { {k: len(v) for k, v in device_map.items()} }")

        # Assign each camera to a unique PNP path based on device_number order
        self._assign_device_paths(device_map)

        # Start streams for cameras whose devices are present
        for name, state in self._states.items():
            if state.device_path:
                state.is_connected = True
                self.start_stream(name)
            else:
                logger.warning(
                    f"[StreamManager] {name}: device '{state.config.device_name}' "
                    f"(index {state.config.device_number}) not found"
                )

        # Start background monitoring threads
        self._hotplug_thread = threading.Thread(
            target=self._hotplug_loop, daemon=True, name="CameraHotplug"
        )
        self._hotplug_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="StreamWatchdog"
        )
        self._watchdog_thread.start()

        logger.info("[StreamManager] Started (hotplug + watchdog active)")

    def stop(self):
        """Stop all streams and monitoring threads."""
        self._running = False
        for name in list(self._states.keys()):
            self.stop_stream(name)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=10)
        if self._hotplug_thread and self._hotplug_thread.is_alive():
            self._hotplug_thread.join(timeout=10)
        logger.info("[StreamManager] Stopped")

    def start_stream(self, camera_name: str) -> bool:
        """Start the ffmpeg process for one camera. Does not affect other cameras."""
        state = self._states.get(camera_name)
        if not state:
            logger.error(f"[StreamManager] Unknown camera: {camera_name}")
            return False

        with state.lock:
            # Kill any leftover process first
            self._kill_process(state, camera_name)

            cfg = state.config
            rtsp_url = f"{self.rtsp_server}/{camera_name}"

            # Use PNP path (unique per USB port) so each ffmpeg grabs
            # exactly the right physical camera regardless of enumeration order.
            if state.device_path:
                input_args = ["-i", f"video={state.device_path}"]
            else:
                input_args = [
                    "-video_device_number", str(cfg.device_number),
                    "-i", f"video={cfg.device_name}",
                ]

            cmd = [
                "ffmpeg",
                "-f", "dshow",
                "-video_size", cfg.video_size,
                "-framerate", str(cfg.framerate),
                *input_args,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-b:v", cfg.bitrate,
                "-g", "60",
                "-pix_fmt", "yuv420p",
                "-an",
                "-f", "rtsp",
                "-rtsp_transport", "tcp",
                rtsp_url,
            ]

            try:
                log_path = f"ffmpeg-{camera_name}.log"
                log_file = open(log_path, "w")  # noqa: SIM115
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                    creationflags=_CREATION_FLAGS,
                )
                state.process = proc
                state.log_file = log_file
                state.healthy = False  # Confirmed later by watchdog
                logger.info(f"[StreamManager] {camera_name} started (PID {proc.pid}) -> {rtsp_url}")

                # Verify the process survives for 3 seconds (catches immediate crashes)
                time.sleep(3)
                if proc.poll() is not None:
                    logger.warning(f"[StreamManager] {camera_name} ffmpeg died immediately (exit {proc.returncode})")
                    state.process = None
                    return False

                return True
            except FileNotFoundError:
                logger.error("[StreamManager] ffmpeg not found on PATH")
                return False
            except Exception as e:
                logger.error(f"[StreamManager] Failed to start {camera_name}: {e}")
                return False

    def stop_stream(self, camera_name: str):
        """Stop the ffmpeg process for one camera. Does not affect other cameras."""
        state = self._states.get(camera_name)
        if not state:
            return

        with state.lock:
            self._kill_process(state, camera_name)
            logger.info(f"[StreamManager] {camera_name} stopped")

    def _kill_process(self, state: CameraState, camera_name: str):
        """Terminate the ffmpeg process and close the log file. Caller must hold state.lock."""
        if state.process:
            try:
                state.process.terminate()
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.process.kill()
                state.process.wait(timeout=3)
            except Exception as e:
                logger.error(f"[StreamManager] Error stopping {camera_name}: {e}")
            finally:
                state.process = None
                state.healthy = False

        if state.log_file:
            try:
                state.log_file.close()
            except Exception:
                pass
            state.log_file = None

    def restart_stream(self, camera_name: str) -> bool:
        """Restart one camera stream (stop + start). Holds the per-camera lock
        for the entire operation so hotplug and watchdog cannot collide."""
        state = self._states.get(camera_name)
        if not state:
            return False

        with state.lock:
            if state.restart_count >= state.max_restarts:
                logger.error(
                    f"[StreamManager] {camera_name} hit max restarts ({state.max_restarts}) — skipping"
                )
                return False

            logger.warning(
                f"[StreamManager] Restarting {camera_name} "
                f"(restart #{state.restart_count + 1}/{state.max_restarts})"
            )
            self._kill_process(state, camera_name)
            logger.info(f"[StreamManager] {camera_name} stopped")

        # Sleep OUTSIDE lock to let device/port settle, but set cooldown
        # so watchdog doesn't race us during the gap
        state.cooldown_until = time.time() + 8
        time.sleep(2)

        result = self.start_stream(camera_name)
        if result:
            state.restart_count += 1
        return result

    def is_stream_healthy(self, camera_name: str) -> bool:
        """True if ffmpeg is running AND go2rtc reports an active producer."""
        state = self._states.get(camera_name)
        if not state:
            return False
        if not state.process or state.process.poll() is not None:
            return False
        return self._has_producer(camera_name)

    def is_stream_running(self, camera_name: str) -> bool:
        """True if the ffmpeg process is alive (may not have a producer yet)."""
        state = self._states.get(camera_name)
        if not state:
            return False
        return state.process is not None and state.process.poll() is None

    def get_mjpeg_url(self, camera_name: str) -> str:
        """Return the go2rtc MJPEG URL for this camera."""
        return f"{self.go2rtc_api}/api/stream.mjpeg?src={camera_name}"

    def get_state(self, camera_name: str) -> Optional[CameraState]:
        return self._states.get(camera_name)

    # ── go2rtc API helpers ──────────────────────────────────────────────

    def _wait_for_api(self, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                requests.get(f"{self.go2rtc_api}/api/streams", timeout=3)
                logger.info(f"[StreamManager] go2rtc API reachable at {self.go2rtc_api}")
                return True
            except Exception:
                time.sleep(1)
        return False

    def _has_producer(self, camera_name: str) -> bool:
        try:
            resp = requests.get(f"{self.go2rtc_api}/api/streams", timeout=5)
            data = resp.json()
            cam_data = data.get(camera_name)
            if cam_data and cam_data.get("producers"):
                for p in cam_data["producers"]:
                    if p.get("id") or p.get("remote_addr") or p.get("format_name"):
                        return True
        except Exception:
            pass
        return False

    # ── Device scanning (DirectShow via ffmpeg) ─────────────────────────

    def _scan_connected_devices(self) -> dict[str, list[str]]:
        """
        Enumerate DirectShow video devices via ffmpeg.
        Returns dict mapping device name -> list of PNP paths.
        Example: {"c922 Pro Stream Webcam": ["@device_pnp_...", "@device_pnp_..."]}
        Each PNP path uniquely identifies a physical camera by USB port.
        """
        devices: dict[str, list[str]] = {}
        try:
            result = subprocess.run(
                ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=_CREATION_FLAGS,
            )
            output = result.stderr
            current_video_device: Optional[str] = None
            for line in output.split("\n"):
                if "Alternative name" in line:
                    match = re.search(r'"(.+?)"', line)
                    if match and current_video_device is not None:
                        path = match.group(1)
                        devices.setdefault(current_video_device, []).append(path)
                    current_video_device = None
                else:
                    match = re.search(r'"(.+?)"\s*\(video\)', line)
                    if match:
                        current_video_device = match.group(1)
        except Exception as e:
            logger.error(f"[StreamManager] Device scan failed: {e}")
        return devices

    def _assign_device_paths(self, device_map: dict[str, list[str]]):
        """Assign PNP paths to cameras based on device_number ordering.
        device_number=0 → first PNP path, device_number=1 → second, etc."""
        by_device: dict[str, list[CameraState]] = {}
        for state in self._states.values():
            by_device.setdefault(state.config.device_name, []).append(state)

        for device_name, states in by_device.items():
            paths = device_map.get(device_name, [])
            states.sort(key=lambda s: s.config.device_number)
            for state in states:
                idx = state.config.device_number
                if idx < len(paths):
                    state.device_path = paths[idx]
                    logger.info(
                        f"[StreamManager] {state.config.name} -> "
                        f"...{state.device_path[-50:]}"
                    )
                else:
                    state.device_path = ""

    # ── Hotplug loop ────────────────────────────────────────────────────

    def _hotplug_loop(self):
        """Periodically scan devices and react to connect/disconnect events.
        Uses PNP paths to uniquely identify cameras — disconnecting one
        camera never affects the other."""
        while self._running:
            time.sleep(self.hotplug_scan_interval)
            if not self._running:
                break

            device_map = self._scan_connected_devices()

            for name, state in self._states.items():
                current_paths = device_map.get(state.config.device_name, [])

                if state.device_path:
                    # ── Camera has an assigned PNP path — check if still present ──
                    if state.device_path not in current_paths:
                        logger.warning(f"[Hotplug] {name} device disconnected")
                        state.is_connected = False
                        state.device_path = ""
                        state.cooldown_until = time.time() + 10
                        self.stop_stream(name)
                        if self.on_camera_disconnected:
                            try:
                                self.on_camera_disconnected(name)
                            except Exception as e:
                                logger.error(f"[Hotplug] on_camera_disconnected callback error: {e}")
                else:
                    # ── Camera has no path — look for a newly plugged device ──
                    assigned_paths = {
                        s.device_path for s in self._states.values() if s.device_path
                    }
                    available = [p for p in current_paths if p not in assigned_paths]
                    if available:
                        state.device_path = available[0]
                        logger.info(
                            f"[Hotplug] {name} device connected "
                            f"(path: ...{state.device_path[-50:]})"
                        )
                        state.is_connected = True
                        state.restart_count = 0
                        state.unhealthy_streak = 0
                        state.cooldown_until = time.time() + 15
                        logger.info(f"[Hotplug] {name} waiting 5s for device to initialise...")
                        time.sleep(5)
                        self.stop_stream(name)  # Kill any zombie ffmpeg
                        time.sleep(1)
                        self.start_stream(name)
                        if self.on_camera_connected:
                            try:
                                self.on_camera_connected(name)
                            except Exception as e:
                                logger.error(f"[Hotplug] on_camera_connected callback error: {e}")

    # ── Watchdog loop ───────────────────────────────────────────────────

    def _watchdog_loop(self):
        """Monitor streams and restart any that have crashed."""
        while self._running:
            time.sleep(self.health_check_interval)
            if not self._running:
                break

            for name, state in self._states.items():
                if not state.is_connected:
                    continue

                # Skip cameras in cooldown (hotplug is handling them)
                if time.time() < state.cooldown_until:
                    continue

                process_alive = state.process is not None and state.process.poll() is None
                has_producer = self._has_producer(name)

                if process_alive and has_producer:
                    # ── Healthy ──
                    state.healthy = True
                    state.unhealthy_streak = 0
                    state.healthy_streak += 1
                    if (
                        state.restart_count > 0
                        and state.healthy_streak >= self.healthy_checks_before_reset
                    ):
                        state.restart_count = 0
                        logger.info(f"[Watchdog] {name} stable — restart counter reset")

                elif not process_alive:
                    # ── Process died ──
                    state.healthy = False
                    state.healthy_streak = 0
                    logger.warning(f"[Watchdog] {name} ffmpeg process died — restarting")
                    self.restart_stream(name)
                    state.unhealthy_streak = 0

                else:
                    # ── Process alive but no producer ──
                    state.healthy = False
                    state.healthy_streak = 0
                    state.unhealthy_streak += 1
                    streak = state.unhealthy_streak
                    if streak >= self.unhealthy_checks_before_restart:
                        logger.warning(
                            f"[Watchdog] {name} no producer for {streak} checks — restarting"
                        )
                        self.restart_stream(name)
                        state.unhealthy_streak = 0
                    else:
                        logger.info(
                            f"[Watchdog] {name} no producer — "
                            f"recheck {streak}/{self.unhealthy_checks_before_restart}"
                        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cameras = {
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

    def on_connected(name: str):
        logger.info(f"[CALLBACK] Camera connected: {name}")

    def on_disconnected(name: str):
        logger.warning(f"[CALLBACK] Camera disconnected: {name}")

    manager = CameraStreamManager(
        cameras=cameras,
        on_camera_connected=on_connected,
        on_camera_disconnected=on_disconnected,
    )

    try:
        manager.start()
        logger.info("Manager running — press Ctrl+C to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        manager.stop()
