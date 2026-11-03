"""
Windows Idle Monitor for Tuya / Smart Life RGB Controller.
Tracks user inactivity (mouse movements, clicks, keyboard events) via Win32 GetLastInputInfo.
Turns off the RGB strip when the PC is idle for longer than the configured timeout,
and automatically turns it back on when the user resumes activity.
"""

import sys
import time
import threading
import ctypes
import logging
from typing import Optional, Callable

logger = logging.getLogger("IdleMonitor")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    """Returns elapsed seconds since the last keyboard or mouse input on Windows."""
    if sys.platform != "win32":
        return 0.0
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
            dw_time = lii.dwTime & 0xFFFFFFFF
            elapsed_ms = (tick - dw_time) & 0xFFFFFFFF
            return elapsed_ms / 1000.0
    except Exception as e:
        logger.debug(f"Error getting idle info: {e}")
    return 0.0


class IdleMonitor:
    def __init__(self, controller, on_state_change: Optional[Callable[[str], None]] = None):
        self.controller = controller
        self.on_state_change = on_state_change
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self.turned_off_by_idle = False
        self._grace_period_until = 0.0
        self._last_known_power_before_idle = True

    def start(self):
        """Starts the background idle monitor thread."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="IdleMonitorThread")
        self._thread.start()
        logger.info("IdleMonitor thread started")

    def stop(self):
        """Stops the idle monitor thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def notify_manual_action(self):
        """Called when user manually interacts with light (GUI, Tray, Siri) to grant a grace period."""
        self._grace_period_until = time.time() + 60.0  # 1 minute grace period
        if not self.controller.power:
            # If user intentionally turned off the strip, don't auto-wake on next mouse move
            self.turned_off_by_idle = False

    def get_status_info(self) -> dict:
        """Returns diagnostic dict for UI display."""
        idle_s = get_idle_seconds()
        cfg = self.controller.config
        enabled = bool(cfg.get("idle_off_enabled", False))
        timeout_m = float(cfg.get("idle_off_minutes", 15))
        timeout_s = timeout_m * 60.0
        wake_on_input = bool(cfg.get("idle_wake_on_input", True))

        return {
            "enabled": enabled,
            "idle_seconds": idle_s,
            "timeout_seconds": timeout_s,
            "timeout_minutes": timeout_m,
            "wake_on_input": wake_on_input,
            "turned_off_by_idle": self.turned_off_by_idle,
            "strip_power": self.controller.power
        }

    def _run(self):
        while self._running:
            try:
                cfg = self.controller.config
                enabled = bool(cfg.get("idle_off_enabled", False))
                timeout_minutes = float(cfg.get("idle_off_minutes", 15))
                wake_on_input = bool(cfg.get("idle_wake_on_input", True))

                if enabled and timeout_minutes > 0:
                    timeout_sec = timeout_minutes * 60.0
                    idle_sec = get_idle_seconds()
                    now = time.time()

                    # Check if user has been idle for >= timeout
                    if idle_sec >= timeout_sec:
                        if now >= self._grace_period_until:
                            if self.controller.power and not self.turned_off_by_idle:
                                logger.info(
                                    f"[IdleMonitor] Inactivity detected ({idle_sec:.1f}s >= {timeout_sec:.1f}s). "
                                    f"Turning OFF RGB strip."
                                )
                                self._last_known_power_before_idle = True
                                self.turned_off_by_idle = True
                                self.controller.set_power(False)
                                if self.on_state_change:
                                    self.on_state_change("idle_off")

                    else:
                        # User is currently active (idle_sec < timeout_sec)
                        if self.turned_off_by_idle:
                            # User was idle and has just moved mouse or pressed a key!
                            # Active threshold: idle_sec is small (e.g. < 5 seconds)
                            if idle_sec <= 5.0:
                                self.turned_off_by_idle = False
                                if wake_on_input and self._last_known_power_before_idle:
                                    logger.info(
                                        f"[IdleMonitor] User resumed activity (idle: {idle_sec:.1f}s). "
                                        f"Turning RGB strip back ON."
                                    )
                                    self.controller.set_power(True)
                                    if self.on_state_change:
                                        self.on_state_change("active_wake")
                else:
                    if self.turned_off_by_idle:
                        self.turned_off_by_idle = False

            except Exception as e:
                logger.error(f"[IdleMonitor] Exception in loop: {e}")

            time.sleep(1.0)
