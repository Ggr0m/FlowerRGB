"""
Schedule and Sun Tracker Module for Tuya / Smart Life RGB Strip Controller.
Features:
- Fixed time scheduling (turn on at HH:MM, turn off at HH:MM)
- City-based sunset/sunrise tracking (auto turn on when dark, auto turn off at dawn)
- Online geocoding & astronomical API via Open-Meteo with offline mathematical fallback
- Background scheduling daemon with state preservation
"""

import math
import time
import json
import urllib.request
import urllib.parse
import threading
import datetime
import logging
from typing import Optional, Tuple, Dict, Any, Callable

logger = logging.getLogger("Scheduler")


# Built-in popular Russian and world cities coordinates (lat, lon) for instant offline accuracy
DEFAULT_CITIES: Dict[str, Tuple[float, float]] = {
    "москва": (55.7558, 37.6173),
    "moscow": (55.7558, 37.6173),
    "санкт-петербург": (59.9343, 30.3351),
    "питер": (59.9343, 30.3351),
    "новосибирск": (55.0084, 82.9357),
    "екатеринбург": (56.8389, 60.6057),
    "казань": (55.7961, 49.1064),
    "нижний новгород": (56.2965, 43.9361),
    "челябинск": (55.1644, 61.4368),
    "самара": (53.1959, 50.1002),
    "омск": (54.9885, 73.3242),
    "ростов-на-дону": (47.2357, 39.7015),
    "уфа": (54.7388, 55.9721),
    "красноярск": (56.0153, 92.8932),
    "воронеж": (51.6755, 39.2089),
    "пермь": (58.0105, 56.2502),
    "волгоград": (48.7080, 44.5133),
    "краснодар": (45.0355, 38.9753),
    "саратов": (51.5406, 46.0086),
    "тюмень": (57.1522, 65.5272),
    "тольятти": (53.5303, 49.3461),
    "ижевск": (56.8498, 53.2045),
    "барнаул": (53.3606, 83.7636),
    "ульяновск": (54.3142, 48.4031),
    "иркутск": (52.2870, 104.3050),
    "хабаровск": (48.4827, 135.0840),
    "ярославль": (57.6261, 39.8845),
    "владивосток": (43.1155, 131.8855),
    "махачкала": (42.9849, 47.5046),
    "томск": (56.4977, 84.9744),
    "оренбург": (51.7666, 55.0998),
    "кемерово": (55.3547, 86.0872),
    "новокузнецк": (53.7596, 87.1216),
    "рязань": (54.6269, 39.6916),
    "астрахань": (46.3497, 48.0408),
    "пенза": (53.2273, 45.0000),
    "киров": (58.6035, 49.6679),
    "липецк": (52.6103, 39.5947),
    "тула": (54.1961, 37.6182),
    "чебоксары": (56.1439, 47.2489),
    "калининград": (54.7104, 20.4522),
    "сочи": (43.6028, 39.7342),
    "минск": (53.9006, 27.5590),
    "алматы": (43.2220, 76.8512),
    "астана": (51.1694, 71.4491),
}


def calculate_sun_times_offline(lat: float, lon: float, date: Optional[datetime.date] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Standard NOAA astronomical formula for sunrise and sunset calculation.
    Returns (sunrise_time_str, sunset_time_str) in HH:MM local time.
    """
    if date is None:
        date = datetime.date.today()
    day_of_year = date.timetuple().tm_yday
    
    # Fractional year in radians
    gamma = 2 * math.pi / 365.0 * (day_of_year - 1)
    
    # Solar declination in radians
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
    )
    
    # Equation of time in minutes
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    
    # Standard zenith for sunrise/sunset (90.833 degrees accounts for atmospheric refraction)
    zenith = math.radians(90.833)
    lat_rad = math.radians(lat)
    
    cos_ha = (math.cos(zenith) - math.sin(lat_rad) * math.sin(decl)) / (math.cos(lat_rad) * math.cos(decl))
    if cos_ha > 1.0:
        return None, None  # Polar night (sun never rises)
    if cos_ha < -1.0:
        return None, None  # Midnight sun (sun never sets)
        
    ha = math.degrees(math.acos(cos_ha))
    
    # Solar noon in UTC minutes
    noon_utc = 720.0 - 4.0 * lon - eqtime
    sunrise_utc_min = noon_utc - ha * 4.0
    sunset_utc_min = noon_utc + ha * 4.0
    
    # Local timezone difference from UTC in minutes
    now = datetime.datetime.now()
    utcnow = datetime.datetime.utcnow()
    tz_offset_min = (now - utcnow).total_seconds() / 60.0
    
    sunrise_local = (sunrise_utc_min + tz_offset_min) / 60.0
    sunset_local = (sunset_utc_min + tz_offset_min) / 60.0
    
    sr_h = int(sunrise_local) % 24
    sr_m = int((sunrise_local * 60) % 60)
    ss_h = int(sunset_local) % 24
    ss_m = int((sunset_local * 60) % 60)
    
    return f"{sr_h:02d}:{sr_m:02d}", f"{ss_h:02d}:{ss_m:02d}"


def resolve_city_coordinates(city_name: str) -> Optional[Tuple[str, float, float]]:
    """
    Resolves city name to (display_name, lat, lon).
    Checks offline dictionary first, then queries Open-Meteo Geocoding API.
    """
    clean_name = city_name.strip().lower()
    if not clean_name:
        return None
        
    if clean_name in DEFAULT_CITIES:
        lat, lon = DEFAULT_CITIES[clean_name]
        return city_name.strip().title(), lat, lon
        
    # Query Open-Meteo Geocoding API
    try:
        encoded = urllib.parse.quote(city_name.strip())
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded}&count=1&language=ru&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartLifeRGB/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and "results" in data and len(data["results"]) > 0:
                res = data["results"][0]
                disp_name = res.get("name", city_name.strip())
                lat = float(res["latitude"])
                lon = float(res["longitude"])
                return disp_name, lat, lon
    except Exception as e:
        logger.debug(f"Geocoding API request failed for '{city_name}': {e}")
        
    return None


def fetch_sun_times_online(lat: float, lon: float) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches exact today's sunrise and sunset times from Open-Meteo API.
    Falls back to offline NOAA calculations if network is unavailable.
    """
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=sunrise,sunset&timezone=auto&forecast_days=1"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartLifeRGB/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "daily" in data:
                sr_iso = data["daily"]["sunrise"][0]  # e.g. "2026-11-01T07:35"
                ss_iso = data["daily"]["sunset"][0]   # e.g. "2026-11-01T16:50"
                sr_time = sr_iso.split("T")[1][:5]
                ss_time = ss_iso.split("T")[1][:5]
                return sr_time, ss_time
    except Exception as e:
        logger.debug(f"Open-Meteo sun API failed: {e}. Using offline calculation.")
        
    return calculate_sun_times_offline(lat, lon)


class ScheduleTracker:
    def __init__(self, controller, on_status_change: Optional[Callable[[str], None]] = None):
        self.controller = controller
        self.on_status_change = on_status_change
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Cached values
        self.cached_sunrise = "--:--"
        self.cached_sunset = "--:--"
        self.resolved_city_name = ""
        self.last_sun_fetch_day: Optional[datetime.date] = None
        
        # Debounce triggered minute to prevent repeated triggers in the same minute
        self._last_triggered_on_minute = ""
        self._last_triggered_off_minute = ""

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ScheduleTrackerThread")
        self._thread.start()
        logger.info("ScheduleTracker started")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def refresh_sun_times(self, force: bool = False):
        """Fetches sunrise and sunset based on configured city or lat/lon."""
        cfg = self.controller.config
        city = cfg.get("schedule_city", "").strip()
        if not city:
            city = "Москва"
            cfg["schedule_city"] = city

        today = datetime.date.today()
        if not force and self.last_sun_fetch_day == today and self.cached_sunrise != "--:--":
            return

        geo = resolve_city_coordinates(city)
        if geo:
            disp_name, lat, lon = geo
            self.resolved_city_name = disp_name
            cfg["schedule_city_lat"] = lat
            cfg["schedule_city_lon"] = lon
            sr, ss = fetch_sun_times_online(lat, lon)
            if sr and ss:
                self.cached_sunrise = sr
                self.cached_sunset = ss
                self.last_sun_fetch_day = today
                logger.info(f"[Schedule] Updated for {disp_name}: Dawn {sr}, Dusk {ss}")
                if self.on_status_change:
                    self.on_status_change(f"Восход: {sr}, Закат: {ss}")
        else:
            # Fallback to Moscow
            sr, ss = calculate_sun_times_offline(55.7558, 37.6173)
            self.cached_sunrise = sr or "07:30"
            self.cached_sunset = ss or "17:00"
            self.resolved_city_name = city

    def get_info(self) -> dict:
        cfg = self.controller.config
        mode = cfg.get("schedule_mode", "sun")  # "off", "fixed", "sun"
        on_time = cfg.get("schedule_on_time", "18:00")
        off_time = cfg.get("schedule_off_time", "23:30")
        city = cfg.get("schedule_city", "Москва")
        
        effective_on = on_time if mode == "fixed" else self.cached_sunset
        effective_off = off_time if mode == "fixed" else self.cached_sunrise
        
        return {
            "mode": mode,
            "city": city,
            "resolved_city": self.resolved_city_name or city,
            "fixed_on": on_time,
            "fixed_off": off_time,
            "sunrise": self.cached_sunrise,
            "sunset": self.cached_sunset,
            "effective_on": effective_on,
            "effective_off": effective_off,
        }

    def _run_loop(self):
        # Initial refresh
        self.refresh_sun_times(force=True)
        
        while self._running:
            try:
                now = datetime.datetime.now()
                today = now.date()
                cur_hm = now.strftime("%H:%M")
                
                # Daily update at midnight or if day changed
                if self.last_sun_fetch_day != today:
                    self.refresh_sun_times(force=True)
                
                cfg = self.controller.config
                mode = cfg.get("schedule_mode", "off")  # "off", "fixed", "sun"
                
                if mode != "off":
                    target_on = ""
                    target_off = ""
                    
                    if mode == "fixed":
                        target_on = cfg.get("schedule_on_time", "").strip()
                        target_off = cfg.get("schedule_off_time", "").strip()
                    elif mode == "sun":
                        # Dusk: turn strip ON
                        target_on = self.cached_sunset
                        # Dawn: turn strip OFF
                        target_off = self.cached_sunrise
                    
                    # Check Turn ON
                    if target_on and cur_hm == target_on:
                        if self._last_triggered_on_minute != cur_hm:
                            self._last_triggered_on_minute = cur_hm
                            if not self.controller.power:
                                logger.info(f"[Schedule] Triggering auto TURN ON ({mode} mode at {cur_hm})...")
                                self.controller.set_power(True)
                                if self.on_status_change:
                                    self.on_status_change(f"Автоматически включено по расписанию ({cur_hm})")
                                    
                    # Check Turn OFF
                    if target_off and cur_hm == target_off:
                        if self._last_triggered_off_minute != cur_hm:
                            self._last_triggered_off_minute = cur_hm
                            if self.controller.power:
                                logger.info(f"[Schedule] Triggering auto TURN OFF ({mode} mode at {cur_hm})...")
                                self.controller.set_power(False)
                                if self.on_status_change:
                                    self.on_status_change(f"Автоматически выключено по расписанию ({cur_hm})")
                                    
            except Exception as e:
                logger.error(f"[Schedule] Error in tracker loop: {e}")
                
            time.sleep(10.0)
