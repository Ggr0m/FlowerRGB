"""
Apple HomeKit Bridge for Smart Life Tuya RGB Strip.
Uses HAP-python to bridge the lightstrip into Apple HomeKit, enabling full control
via the Apple Home app on iPhone/iPad/Mac and Siri voice commands ("Привет, Сири...").
"""

import os
import sys
import time
import socket
import asyncio
import threading
import colorsys
import logging
from typing import Callable, Optional, Tuple, Dict, Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB
from zeroconf.asyncio import AsyncZeroconf

import pyqrcode
from PIL import Image, ImageDraw

logger = logging.getLogger("HomeKitBridge")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homekit.state")
DEFAULT_PIN = b"031-45-154"


def get_local_wifi_ip() -> str:
    """Finds the local IP address routable to the home router."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "0.0.0.0"


class TuyaHomeKitLight(Accessory):
    category = CATEGORY_LIGHTBULB

    def __init__(self, driver: AccessoryDriver, controller, name: str = "RGB Лента"):
        super().__init__(driver, name)
        self.controller = controller

        # Model and manufacturer info
        serv_info = self.get_service("AccessoryInformation")
        if serv_info:
            serv_info.configure_char("Manufacturer", value="Smart Life")
            serv_info.configure_char("Model", value="Tuya RGB Strip")
            serv_info.configure_char("SerialNumber", value="TUYA-RGB-001")
            serv_info.configure_char("FirmwareRevision", value="1.0.0")

        # 1. Create Lightbulb service and add characteristics BEFORE adding service to Accessory
        serv = self.driver.loader.get_service("Lightbulb")
        for c_name in ["Brightness", "Hue", "Saturation"]:
            serv.add_characteristic(self.driver.loader.get_char(c_name))

        # 2. Add service to accessory
        self.add_service(serv)

        # 3. Configure characteristics with callbacks
        self.char_on = serv.configure_char(
            "On",
            setter_callback=self._set_on,
            value=1 if controller.power else 0
        )

        self.char_bright = serv.configure_char(
            "Brightness",
            setter_callback=self._set_bright,
            value=max(1, min(100, controller.brightness))
        )

        r, g, b = controller.color
        h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        self._current_hue = float(h * 360.0)
        self._current_sat = float(s * 100.0)

        self.char_hue = serv.configure_char("Hue", setter_callback=self._set_hue, value=self._current_hue)
        self.char_sat = serv.configure_char("Saturation", setter_callback=self._set_sat, value=self._current_sat)

        # Color debounce
        self._color_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _set_on(self, value):
        logger.info(f"[HomeKit] Siri/Home set power to: {value}")
        self.controller.set_power(bool(value))

    def _set_bright(self, value):
        logger.info(f"[HomeKit] Siri/Home set brightness to: {value}%")
        self.controller.set_brightness(int(value))

    def _set_hue(self, value):
        self._current_hue = float(value)
        self._schedule_color_update()

    def _set_sat(self, value):
        self._current_sat = float(value)
        self._schedule_color_update()

    def _schedule_color_update(self):
        with self._lock:
            if self._color_timer:
                self._color_timer.cancel()
            self._color_timer = threading.Timer(0.08, self._apply_hs_color)
            self._color_timer.daemon = True
            self._color_timer.start()

    def _apply_hs_color(self):
        h = self._current_hue / 360.0
        s = self._current_sat / 100.0

        # If user picked white / neutral in Apple Home (s < 0.12), use calibrated white (#FFD7D7)
        if s < 0.12:
            logger.info("[HomeKit] White chosen -> applying calibrated white (#FFD7D7)")
            self.controller.set_color(255, 215, 215, immediate=True)
        else:
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            logger.info(f"[HomeKit] Color set: RGB({int(r*255)}, {int(g*255)}, {int(b*255)})")
            self.controller.set_color(int(r * 255), int(g * 255), int(b * 255), immediate=True)

    def sync_from_controller(self, state: dict):
        """Updates HomeKit characteristics when changed from PC to keep iOS in sync."""
        try:
            power = state.get("power", True)
            bright = state.get("brightness", 100)
            color = state.get("color", (255, 215, 215))

            on_val = 1 if power else 0
            if self.char_on.value != on_val:
                self.char_on.set_value(on_val)

            if self.char_bright.value != bright:
                self.char_bright.set_value(bright)

            r, g, b = color
            h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            new_hue = float(h * 360.0)
            new_sat = float(s * 100.0)
            if abs(self.char_hue.value - new_hue) > 4:
                self.char_hue.set_value(new_hue)
            if abs(self.char_sat.value - new_sat) > 4:
                self.char_sat.set_value(new_sat)
        except Exception as e:
            logger.error(f"[HomeKit] Error syncing state: {e}")


class HomeKitBridge:
    def __init__(self, controller, on_status_change: Optional[Callable[[bool, str], None]] = None):
        self.controller = controller
        self.on_status_change = on_status_change

        self.driver: Optional[AccessoryDriver] = None
        self.accessory: Optional[TuyaHomeKitLight] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        self.is_running = False
        self.pin_str = "031-45-154"
        self.xhm_uri = ""
        self.qr_image: Optional[Image.Image] = None

    def start(self, port: int = 51827):
        if self.is_running:
            return

        self._thread = threading.Thread(target=self._run_driver, args=(port,), daemon=True)
        self._thread.start()

    def _run_driver(self, port: int):
        try:
            self.loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(self.loop)

            device_name = self.controller.config.get("device_name", "RGB Лента")
            local_ip = get_local_wifi_ip()
            logger.info(f"[HomeKit] Binding to Wi-Fi IP {local_ip}:{port}")

            self.azc = AsyncZeroconf()
            self.driver = AccessoryDriver(
                address=local_ip,
                port=port,
                persist_file=STATE_FILE,
                pincode=DEFAULT_PIN,
                loop=self.loop,
                async_zeroconf_instance=self.azc
            )

            self.accessory = TuyaHomeKitLight(self.driver, self.controller, name=device_name)
            self.driver.add_accessory(self.accessory)

            # Generate QR URI and Pillow QR image
            self.xhm_uri = self.accessory.xhm_uri()
            self.qr_image = self._render_qr_image(self.xhm_uri)

            self.is_running = True
            logger.info(f"[HomeKit] Server ready. PIN: {self.pin_str}, URI: {self.xhm_uri}")

            if self.on_status_change:
                self.on_status_change(True, f"Мост HomeKit активен ({local_ip}:{port})")

            # Driver start blocks the thread until stop()
            self.driver.start()

        except Exception as e:
            self.is_running = False
            logger.error(f"[HomeKit] Driver error: {e}", exc_info=True)
            if self.on_status_change:
                self.on_status_change(False, f"Ошибка HomeKit: {e}")

    def get_qr_data_uri(self) -> str:
        """Returns the HomeKit setup QR code as a base64 PNG data-uri."""
        if not self.qr_image and self.accessory:
            try:
                self.xhm_uri = self.accessory.xhm_uri()
                self.qr_image = self._render_qr_image(self.xhm_uri)
            except Exception:
                pass
        if self.qr_image:
            try:
                import base64
                from io import BytesIO
                buf = BytesIO()
                self.qr_image.save(buf, format="PNG")
                return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
            except Exception:
                pass
        return ""

    def stop(self):
        self.is_running = False
        if self.driver:
            try:
                self.driver.stop()
            except Exception:
                pass
        if hasattr(self, "azc") and self.azc:
            try:
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.azc.async_close(), self.loop)
            except Exception:
                pass
        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass
        if self.on_status_change:
            self.on_status_change(False, "Мост HomeKit остановлен")

    def sync_state(self, state: dict):
        if self.accessory and self.is_running:
            self.accessory.sync_from_controller(state)

    def reset_pairing(self):
        """Deletes homekit.state pairing file so user can re-pair with new devices."""
        self.stop()
        if os.path.exists(STATE_FILE):
            try:
                os.remove(STATE_FILE)
                logger.info("Deleted homekit.state")
            except Exception as e:
                logger.error(f"Error removing homekit.state: {e}")
        time.sleep(0.5)
        self.start()

    @staticmethod
    def _render_qr_image(payload_uri: str, scale: int = 5) -> Image.Image:
        """Renders HomeKit setup QR code directly into a clean Pillow image."""
        try:
            qr = pyqrcode.create(payload_uri)
            matrix = qr.code
            size = len(matrix)
            border = 3
            total_size = (size + border * 2) * scale
            img = Image.new("RGB", (total_size, total_size), "white")
            draw = ImageDraw.Draw(img)

            for y, row in enumerate(matrix):
                for x, val in enumerate(row):
                    if val:
                        x0 = (x + border) * scale
                        y0 = (y + border) * scale
                        draw.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill="black")
            return img
        except Exception as e:
            logger.error(f"Error rendering QR image: {e}")
            return Image.new("RGB", (180, 180), "white")
