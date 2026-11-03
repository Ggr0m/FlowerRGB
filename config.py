"""
Configuration manager for Tuya / Smart Life RGB Controller.
Saves settings to config.json and handles Windows autorun registry.
"""

import json
import os
import sys

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "device_name": "flowerRGB",
    "ip": "",
    "device_id": "",
    "local_key": "",
    "protocol_version": "3.3",
    "connection_mode": "local",  # "local" or "cloud"
    "cloud_client_id": "",
    "cloud_secret": "",
    "cloud_region": "eu",        # 'eu', 'us', 'cn', 'in'
    "cloud_device_id": "",
    "start_minimized": False,
    "autostart": False,
    "last_power": True,
    "last_brightness": 100,
    "last_color": [0, 200, 255], # RGB tuple
    "last_mode": "colour",       # "colour" or "white"
    "last_white_temp": 50,       # 0..100
    "turn_on_on_pc_boot": True,  # Turn ON strip in white (#FFD7D7) when PC boots
    "boot_color": [255, 215, 215],
    "white_color": [255, 215, 215],  # Calibrated white color for presets, boot, and Ambilight
    "turn_off_on_pc_shutdown": True, # Turn OFF strip when PC shuts down
    "idle_off_enabled": False,       # Auto turn off strip on PC idle
    "idle_off_minutes": 15,          # Idle timeout in minutes (any positive number)
    "idle_wake_on_input": True,      # Turn strip back on when user resumes activity
    "schedule_mode": "off",          # "off", "fixed", "sun"
    "schedule_on_time": "18:00",     # Fixed turn on time (HH:MM)
    "schedule_off_time": "23:30",    # Fixed turn off time (HH:MM)
    "schedule_city": "Москва",       # City for automatic dawn/dusk sunrise/sunset
    "schedule_city_lat": 55.7558,
    "schedule_city_lon": 37.6173,
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config.update(saved)
        except Exception as e:
            print(f"[Config] Error loading config.json: {e}")
    return config


def save_config(config: dict) -> bool:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[Config] Error saving config.json: {e}")
        return False


def set_windows_autorun(enable: bool) -> bool:
    """Sets or removes the app from Windows HKCU Run registry."""
    if sys.platform != "win32":
        return False

    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "flowerRGB"

    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
    cfg = load_config()
    min_flag = " --minimized" if cfg.get("start_minimized", False) else ""
    command = f'"{pythonw}" "{script_path}"{min_flag}'

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        print(f"[Config] Autorun registry error: {e}")
        return False


def is_windows_autorun_enabled() -> bool:
    if sys.platform != "win32":
        return False

    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "flowerRGB"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, app_name)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
