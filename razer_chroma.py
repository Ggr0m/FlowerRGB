"""
Razer Chroma SDK integration module.
Communicates with Razer Synapse / Chroma SDK via local REST API (port 54235).
Registers the application in Razer Synapse Chroma Connect so it appears
as an authorized Chroma RGB application.
"""

import time
import threading
import requests
import logging
from typing import Optional, Callable

logger = logging.getLogger("RazerChroma")

CHROMA_URL = "http://localhost:54235/razer/chromasdk"


class RazerChromaClient:
    def __init__(self, on_status_change: Optional[Callable[[bool, str], None]] = None):
        self.on_status_change = on_status_change
        self.is_connected = False
        self.session_uri: Optional[str] = None
        self.session_id: Optional[int] = None

        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        """Initializes connection to local Razer Chroma SDK."""
        app_info = {
            "title": "Smart Life RGB Controller",
            "description": "Синхронизация RGB-ленты Smart Life с Razer Chroma",
            "author": {
                "name": "SmartLife",
                "contact": "support"
            },
            "device_supported": ["keyboard", "chromalink"],
            "category": "application"
        }

        try:
            r = requests.post(CHROMA_URL, json=app_info, timeout=2.0)
            if r.status_code == 200:
                data = r.json()
                self.session_id = data.get("sessionid")
                self.session_uri = data.get("uri")
                self.is_connected = True
                logger.info(f"Connected to Razer Chroma SDK: {self.session_uri}")

                # Start heartbeat thread
                self._stop_event.clear()
                self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                self._heartbeat_thread.start()

                if self.on_status_change:
                    self.on_status_change(True, "Подключено к Razer Chroma SDK")
                return True
            else:
                self.is_connected = False
                msg = f"Chroma SDK HTTP {r.status_code}"
                if self.on_status_change:
                    self.on_status_change(False, msg)
                return False

        except Exception as e:
            self.is_connected = False
            if self.on_status_change:
                self.on_status_change(False, f"Razer Synapse не найден или выключен ({e})")
            return False

    def disconnect(self):
        """Uninitializes Chroma SDK session."""
        self._stop_event.set()
        if self.session_uri:
            try:
                requests.delete(self.session_uri, timeout=1.0)
            except Exception:
                pass
        self.session_uri = None
        self.session_id = None
        self.is_connected = False
        if self.on_status_change:
            self.on_status_change(False, "Отключено")

    def _heartbeat_loop(self):
        """Sends periodic heartbeats to maintain Chroma session."""
        while not self._stop_event.is_set():
            time.sleep(1.0)
            if self.session_uri:
                try:
                    requests.put(f"{self.session_uri}/heartbeat", timeout=1.5)
                except Exception as e:
                    logger.warning(f"Chroma heartbeat failed: {e}")
                    self.is_connected = False
                    if self.on_status_change:
                        self.on_status_change(False, "Связь с Chroma SDK потеряна")
                    break
