"""
Tuya Controller module.
Handles local network socket communication with Tuya / Smart Life RGB strips.
Features:
- Auto IP discovery (finds device in Wi-Fi even if IP changes)
- Full RGB + HSV support for standard Tuya strips and bulbs
- Brightness fallback for RGB-only strips (DPS 24 HSV value scaling)
- Background non-blocking execution thread with debouncing
"""

import time
import threading
import queue
import logging
import colorsys
from typing import Callable, Optional, Dict, Any, List, Tuple

import tinytuya
from config import save_config

logger = logging.getLogger("TuyaController")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class TuyaController:
    def __init__(self, config: dict, on_state_change: Optional[Callable[[dict], None]] = None):
        self.config = config
        self.on_state_change = on_state_change

        self.device: Optional[tinytuya.BulbDevice] = None
        self.cloud: Optional[tinytuya.Cloud] = None

        # Current cached state
        self.is_connected = False
        self.power = self.config.get("last_power", False)
        self.brightness = self.config.get("last_brightness", 100)
        self.color: Tuple[int, int, int] = tuple(self.config.get("last_color", [0, 200, 255]))
        self.mode = self.config.get("last_mode", "colour")
        self.white_temp = self.config.get("last_white_temp", 50)
        self.last_error = ""

        # Flag for RGB-only strips where brightness is embedded in DPS 24
        self.is_hsv_only_strip = False

        # Worker thread & command queue
        self._cmd_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

        # Debounce timer for sliders
        self._debounce_lock = threading.Lock()
        self._pending_commands: Dict[str, Any] = {}
        self._debounce_timer: Optional[threading.Timer] = None

        # Periodic status poller
        self._poller_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller_thread.start()

        # Initialize connection
        self.reconfigure(self.config)

    def reconfigure(self, new_config: dict):
        """Applies new configuration parameters and reconnects."""
        self.config = dict(new_config)
        self.power = self.config.get("last_power", self.power)
        self.brightness = self.config.get("last_brightness", self.brightness)
        self.color = tuple(self.config.get("last_color", self.color))
        self.mode = self.config.get("last_mode", self.mode)
        self.white_temp = self.config.get("last_white_temp", self.white_temp)

        ip = self.config.get("ip", "").strip()
        dev_id = self.config.get("device_id", "").strip()
        local_key = self.config.get("local_key", "").strip()
        version = self.config.get("protocol_version", "3.3").strip()

        # If IP is missing but dev_id is known, attempt background auto-discovery
        if dev_id and local_key and not ip:
            threading.Thread(target=self._auto_discover_ip, daemon=True).start()
            return

        if ip and dev_id and local_key:
            try:
                b = tinytuya.BulbDevice(dev_id, ip, local_key)
                try:
                    ver_float = float(version)
                    b.set_version(ver_float)
                except ValueError:
                    b.set_version(3.3)
                b.set_socketTimeout(2.5)
                b.set_socketRetryLimit(1)
                self.device = b
                logger.info(f"Local BulbDevice configured for IP {ip}, DevID {dev_id[:8]}***")
            except Exception as e:
                self.device = None
                self.last_error = f"Device init error: {e}"
                logger.error(self.last_error)
        else:
            self.device = None

        # Cloud setup
        cloud_client_id = self.config.get("cloud_client_id", "").strip()
        cloud_secret = self.config.get("cloud_secret", "").strip()
        cloud_region = self.config.get("cloud_region", "eu").strip()
        cloud_device_id = self.config.get("cloud_device_id", dev_id).strip()

        if cloud_client_id and cloud_secret:
            try:
                self.cloud = tinytuya.Cloud(
                    apiRegion=cloud_region,
                    apiKey=cloud_client_id,
                    apiSecret=cloud_secret,
                    apiDeviceID=cloud_device_id
                )
            except Exception as e:
                self.cloud = None
                logger.error(f"Tuya Cloud init error: {e}")
        else:
            self.cloud = None

        # Schedule status check
        self.queue_command("check_status", None)

    def _auto_discover_ip(self):
        dev_id = self.config.get("device_id", "").strip()
        if not dev_id:
            return
        logger.info(f"Scanning network to find IP for device {dev_id}...")
        scanned = self.scan_network_devices()
        for dev in scanned:
            if dev.get("gwId") == dev_id:
                found_ip = dev.get("ip")
                logger.info(f"Auto-discovered IP: {found_ip}")
                self.config["ip"] = found_ip
                save_config(self.config)
                self.reconfigure(self.config)
                break

    # -------------------------------------------------------------
    # High-level public actions
    # -------------------------------------------------------------
    def toggle_power(self) -> bool:
        new_power = not self.power
        self.set_power(new_power)
        return new_power

    def set_power(self, power: bool):
        self.power = bool(power)
        self.config["last_power"] = self.power
        self._notify_state()
        self.queue_command("set_power", self.power)

    def turn_off_immediate(self):
        """Immediately and synchronously sends turn_off to the physical strip (for PC shutdown)."""
        logger.info("Executing immediate shutdown of RGB strip...")
        if self.device:
            try:
                self.device.turn_off()
            except Exception as e:
                logger.error(f"Error in turn_off_immediate (local): {e}")
        elif self.cloud:
            try:
                dev_id = self.config.get("cloud_device_id") or self.config.get("device_id")
                if dev_id:
                    self.cloud.sendcommand(dev_id, [{"code": "switch_led", "value": False}])
            except Exception as e:
                logger.error(f"Error in turn_off_immediate (cloud): {e}")

    def boot_sequence(self):
        """Executed on startup when turn_on_on_pc_boot is True."""
        if self.config.get("turn_on_on_pc_boot", True):
            boot_color = tuple(self.config.get("boot_color", [255, 215, 215]))
            logger.info(f"PC boot: turning strip ON with calibrated white {boot_color} at 100%...")
            self.set_power(True)
            self.set_color(boot_color[0], boot_color[1], boot_color[2], brightness=100, immediate=True)

    def set_brightness(self, percent: int, immediate: bool = False):
        percent = max(1, min(100, int(percent)))
        self.brightness = percent
        self.config["last_brightness"] = self.brightness
        self._notify_state()

        if immediate:
            self.queue_command("set_brightness", percent)
        else:
            self._debounce_command("set_brightness", percent, delay=0.08)

    def set_color(self, r: int, g: int, b: int, brightness: Optional[int] = None, immediate: bool = False):
        self.color = (int(r), int(g), int(b))
        self.mode = "colour"
        self.config["last_color"] = list(self.color)
        self.config["last_mode"] = "colour"

        if brightness is not None:
            effective_bright = max(1, min(100, int(brightness)))
            cmd_data = (int(r), int(g), int(b), effective_bright)
        else:
            effective_bright = self.brightness
            self.config["last_brightness"] = self.brightness
            cmd_data = (int(r), int(g), int(b), effective_bright)

        self._notify_state()

        if immediate:
            self.queue_command("set_color", cmd_data)
        else:
            self._debounce_command("set_color", cmd_data, delay=0.08)

    def set_white(self, brightness: int, temp_percent: int = 50):
        self.brightness = max(1, min(100, int(brightness)))
        self.white_temp = max(0, min(100, int(temp_percent)))
        self.mode = "white"
        self.config["last_brightness"] = self.brightness
        self.config["last_white_temp"] = self.white_temp
        self.config["last_mode"] = "white"
        self._notify_state()
        self._debounce_command("set_white", (self.brightness, self.white_temp), delay=0.08)

    # -------------------------------------------------------------
    # Queue and Debouncing
    # -------------------------------------------------------------
    def queue_command(self, action: str, data: Any):
        self._cmd_queue.put((action, data))

    def _debounce_command(self, action: str, data: Any, delay: float = 0.08):
        with self._debounce_lock:
            self._pending_commands[action] = data
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(delay, self._flush_debounce)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _flush_debounce(self):
        with self._debounce_lock:
            for action, data in self._pending_commands.items():
                self.queue_command(action, data)
            self._pending_commands.clear()

    # -------------------------------------------------------------
    # Background Worker Loop
    # -------------------------------------------------------------
    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                action, data = self._cmd_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self._execute_action(action, data)
            except Exception as e:
                logger.error(f"Error executing action {action}: {e}")
                self.last_error = str(e)
            finally:
                self._cmd_queue.task_done()

    def _execute_action(self, action: str, data: Any):
        if not self.device and not self.cloud:
            self.is_connected = False
            self.last_error = "Устройство не настроено (Демо-режим)"
            self._notify_state()
            return

        if self.device:
            self._execute_local(action, data)
        elif self.cloud:
            self._execute_cloud(action, data)

    def _execute_local(self, action: str, data: Any):
        assert self.device is not None
        try:
            if action == "check_status":
                status = self.device.status()
                if isinstance(status, dict) and "dps" in status:
                    self.is_connected = True
                    self.last_error = ""
                    self._parse_status_dps(status["dps"])
                elif isinstance(status, dict) and "Error" in status:
                    self.is_connected = False
                    self.last_error = f"{status.get('Error')} ({status.get('Err')})"
                else:
                    self.is_connected = False
                    self.last_error = "Нет ответа от устройства"

            elif action == "set_power":
                if data:
                    res = self.device.turn_on()
                else:
                    res = self.device.turn_off()
                self._check_res(res)

            elif action == "set_brightness":
                percent = int(data)
                # First try standard brightness if not confirmed HSV-only
                if not self.is_hsv_only_strip:
                    res = self.device.set_brightness_percentage(percent)
                    if isinstance(res, dict) and res.get("Err") == "907":
                        self.is_hsv_only_strip = True

                # If HSV-only strip, scale V in DPS 24
                if self.is_hsv_only_strip:
                    r, g, b = self.color
                    h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                    v = max(0.01, min(1.0, percent / 100.0))
                    res = self.device.set_hsv(h, s, v)

                self._check_res(res)

            elif action == "set_color":
                if len(data) == 4:
                    r, g, b, bright_val = data
                else:
                    r, g, b = data
                    bright_val = self.brightness
                h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                v = max(0.01, min(1.0, bright_val / 100.0))
                # Using set_hsv ensures brightness is also preserved!
                res = self.device.set_hsv(h, s, v)
                self._check_res(res)

            elif action == "set_white":
                bright, temp = data
                res = self.device.set_white_percentage(bright, temp)
                self._check_res(res)

            self._notify_state()

        except Exception as e:
            self.is_connected = False
            self.last_error = str(e)
            logger.error(f"Local communication exception on {action}: {e}")
            self._notify_state()

    def _execute_cloud(self, action: str, data: Any):
        assert self.cloud is not None
        dev_id = self.config.get("cloud_device_id") or self.config.get("device_id")
        if not dev_id:
            self.last_error = "Cloud Device ID отсутствует"
            return

        try:
            if action == "check_status":
                status = self.cloud.getstatus(dev_id)
                if isinstance(status, dict) and status.get("success", False):
                    self.is_connected = True
                    self.last_error = ""
                else:
                    self.is_connected = False
                    self.last_error = str(status.get("msg", "Cloud status failed"))

            elif action == "set_power":
                commands = [{"code": "switch_led", "value": bool(data)}]
                res = self.cloud.sendcommand(dev_id, commands)
                self._check_cloud_res(res)

            elif action == "set_brightness":
                val = int(data * 10)
                commands = [{"code": "bright_value_v2", "value": val}]
                res = self.cloud.sendcommand(dev_id, commands)
                self._check_cloud_res(res)

            elif action == "set_color":
                if len(data) == 4:
                    r, g, b, bright_val = data
                else:
                    r, g, b = data
                    bright_val = self.brightness
                h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                v = max(0.01, min(1.0, bright_val / 100.0))
                commands = [
                    {"code": "work_mode", "value": "colour"},
                    {"code": "colour_data_v2", "value": {"h": int(h * 360), "s": int(s * 1000), "v": int(v * 1000)}}
                ]
                res = self.cloud.sendcommand(dev_id, commands)
                self._check_cloud_res(res)

            self._notify_state()

        except Exception as e:
            self.is_connected = False
            self.last_error = str(e)
            self._notify_state()

    def _check_res(self, res: Any):
        if isinstance(res, dict) and "Error" in res:
            self.is_connected = False
            self.last_error = f"{res.get('Error')} ({res.get('Err')})"
        else:
            self.is_connected = True
            self.last_error = ""

    def _check_cloud_res(self, res: Any):
        if isinstance(res, dict) and res.get("success", False):
            self.is_connected = True
            self.last_error = ""
        else:
            self.is_connected = False
            self.last_error = res.get("msg", "Cloud command failed") if isinstance(res, dict) else str(res)

    def _parse_status_dps(self, dps: dict):
        """Extracts power, mode, brightness, and color from reported DPS."""
        # Power DPS 20 or 1
        if "20" in dps:
            self.power = bool(dps["20"])
        elif "1" in dps:
            self.power = bool(dps["1"])

        # Mode DPS 21 or 2
        if "21" in dps:
            self.mode = str(dps["21"])
        elif "2" in dps:
            self.mode = str(dps["2"])

        # Color DPS 24 (hhhhssssvvvv)
        colour_hex = dps.get("24")
        if colour_hex and isinstance(colour_hex, str) and len(colour_hex) == 12:
            try:
                h = int(colour_hex[0:4], 16) / 360.0
                s = int(colour_hex[4:8], 16) / 1000.0
                v = int(colour_hex[8:12], 16) / 1000.0
                r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)  # Pure hue/sat color
                self.color = (int(r * 255), int(g * 255), int(b * 255))
                self.brightness = max(1, min(100, int(v * 100)))
                self.is_hsv_only_strip = True
            except Exception as e:
                logger.error(f"Error parsing DPS 24: {e}")

        # Brightness DPS 22 if present
        b_raw = dps.get("22", dps.get("3"))
        if b_raw is not None and isinstance(b_raw, (int, float)):
            if b_raw > 100:
                self.brightness = int(b_raw / 10)
            else:
                self.brightness = int(b_raw)
            self.is_hsv_only_strip = False

    def _notify_state(self):
        if self.on_state_change:
            try:
                state_snapshot = {
                    "is_connected": self.is_connected,
                    "power": self.power,
                    "brightness": self.brightness,
                    "color": self.color,
                    "mode": self.mode,
                    "white_temp": self.white_temp,
                    "last_error": self.last_error
                }
                self.on_state_change(state_snapshot)
            except Exception as e:
                logger.error(f"Error in on_state_change callback: {e}")

    # -------------------------------------------------------------
    # Poller loop
    # -------------------------------------------------------------
    def _poll_loop(self):
        while not self._stop_event.is_set():
            time.sleep(10)
            if self.device:
                self.queue_command("check_status", None)

    # -------------------------------------------------------------
    # Discovery & Cloud Helpers
    # -------------------------------------------------------------
    @staticmethod
    def scan_network_devices(timeout: int = 5) -> List[Dict[str, Any]]:
        """Scans local Wi-Fi for Tuya devices via UDP broadcast."""
        try:
            raw_devices = tinytuya.deviceScan(verbose=False, maxretry=2, color=False, poll=False)
            results = []
            if isinstance(raw_devices, dict):
                for ip, info in raw_devices.items():
                    results.append({
                        "ip": ip,
                        "gwId": info.get("gwId", ""),
                        "productKey": info.get("productKey", ""),
                        "version": info.get("version", "3.3")
                    })
            return results
        except Exception as e:
            logger.error(f"Scan error: {e}")
            return []

    @staticmethod
    def fetch_devices_from_cloud(region: str, client_id: str, secret: str) -> List[Dict[str, Any]]:
        """Connects to Tuya Cloud and fetches paired devices with their Local Keys."""
        try:
            cloud = tinytuya.Cloud(
                apiRegion=region,
                apiKey=client_id,
                apiSecret=secret
            )
            devices = cloud.getdevices()
            output = []
            if isinstance(devices, list):
                for dev in devices:
                    if isinstance(dev, dict):
                        output.append({
                            "name": dev.get("name", "Неизвестное устройство"),
                            "id": dev.get("id", ""),
                            "key": dev.get("key", ""),
                            "ip": dev.get("ip", ""),
                            "category": dev.get("category", "")
                        })
            return output
        except Exception as e:
            logger.error(f"Cloud fetch error: {e}")
            raise e

    def stop(self):
        self._stop_event.set()
        if self._debounce_timer:
            self._debounce_timer.cancel()
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
