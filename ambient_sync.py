"""
Ambient Screen Sync (Ambilight) Engine with Temporal Supersampling & Intelligent Scene Adaptation.
Captures screen colors in real time and streams them to the Tuya RGB strip.

Features:
- Color Simplification & Dominant Hue Clustering:
  * When multiple colors are present, prioritizes the dominant hue cluster instead of washing out to grey/white.
- Clutter Protection (Drop to 1%):
  * When screen has too many conflicting colors (chaotic multi-color clutter with no dominant color),
    automatically drops brightness to 1% instead of glowing in harsh moon-white.
  * As soon as a clear, single-tone or dominant color appears, smoothly restores full brightness.
- Letterbox & Black Screen Protection (Drop to 1%):
  * When > 70% of the capture zone is black, drops brightness to 1% (prevents glowing during dark cutscenes).
- Accurate Monitor Refresh Rate (e.g. 144Hz, 240Hz, 310Hz) & 1ms multimedia timer.
- Temporal Supersampling:
  * Reads EVERY single frame from the monitor at native refresh rate.
  * Accumulates and temporally integrates all intermediate frames between Wi-Fi network updates (~35Hz).
- Dynamic scene brightness adaptation with forced base_brightness (50% .. 100%).
- White calibration support (#FFD7D7 as pure neutral white).
- Exponential smoothing for buttery transitions.
"""

import time
import threading
import ctypes
from ctypes import wintypes
import colorsys
from typing import Callable, Optional, Tuple, Dict, Any

from tuya_controller import TuyaController

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    winmm = ctypes.windll.winmm
except Exception:
    winmm = None


def get_monitor_refresh_rate() -> int:
    """Detects primary monitor hardware refresh rate via EnumDisplaySettingsW."""
    try:
        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ('dmDeviceName', wintypes.WCHAR * 32),
                ('dmSpecVersion', wintypes.WORD),
                ('dmDriverVersion', wintypes.WORD),
                ('dmSize', wintypes.WORD),
                ('dmDriverExtra', wintypes.WORD),
                ('dmFields', wintypes.DWORD),
                ('dmOrientation', ctypes.c_short),
                ('dmPaperSize', ctypes.c_short),
                ('dmPaperLength', ctypes.c_short),
                ('dmPaperWidth', ctypes.c_short),
                ('dmScale', ctypes.c_short),
                ('dmCopies', ctypes.c_short),
                ('dmDefaultSource', ctypes.c_short),
                ('dmPrintQuality', ctypes.c_short),
                ('dmColor', ctypes.c_short),
                ('dmDuplex', ctypes.c_short),
                ('dmYResolution', ctypes.c_short),
                ('dmTTOption', ctypes.c_short),
                ('dmCollate', ctypes.c_short),
                ('dmFormName', wintypes.WCHAR * 32),
                ('dmLogPixels', wintypes.WORD),
                ('dmBitsPerPel', wintypes.DWORD),
                ('dmPelsWidth', wintypes.DWORD),
                ('dmPelsHeight', wintypes.DWORD),
                ('dmDisplayFlags', wintypes.DWORD),
                ('dmDisplayFrequency', wintypes.DWORD),
            ]
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            freq = int(dm.dmDisplayFrequency)
            if freq > 1:
                return freq
    except Exception:
        pass
    return 60


class AmbientSyncEngine:
    def __init__(self, controller: TuyaController, on_preview_update: Optional[Callable[[Tuple[int, int, int], int, float, int, float, bool], None]] = None):
        self.controller = controller
        self.on_preview_update = on_preview_update

        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Hardware display refresh rate
        self.monitor_hz = get_monitor_refresh_rate()

        # Capture & Timing Settings
        self.vsync = True     # Synchronizes with detected monitor refresh rate
        self.fps = 60         # 15 .. 120 FPS when manual mode
        self.min_send_interval = 0.028  # ~35 packets/sec max network rate for Wi-Fi microcontroller

        # Color & Tuning Settings
        self.zone = "bottom"  # "whole", "top", "bottom", "center", "custom"
        self.saturation_boost = 1.35
        self.smoothing = 0.35 # 0.1 (very smooth) .. 0.9 (instant)
        self.custom_rect: Optional[Tuple[int, int, int, int]] = None  # (x1, y1, x2, y2)

        # Dynamic brightness configuration
        self.min_brightness = 15   # Normal scene floor: 15%
        self.max_brightness = 100  # Ceiling: 100%
        self.gamma = 0.55

        # White calibration target: from config or default #FFD7D7 = (255, 215, 215)
        self.calibrated_white = tuple(self.controller.config.get("white_color", [255, 215, 215]))

        # Internal state & telemetry
        self.measured_fps = float(self.monitor_hz)
        self.supersample_count = max(1, round(self.monitor_hz * self.min_send_interval))
        self.last_black_ratio = 0.0
        self.last_is_clutter = False
        self.simplify_colors = False

        self._curr_r = 0.0
        self._curr_g = 0.0
        self._curr_b = 0.0
        self._curr_brightness = 100.0
        self._last_sent_rgb = (0, 0, 0)
        self._last_sent_brightness = -1
        self._last_sent_time = 0.0
        self._pause_until = 0.0

    def pause_stream(self, duration: float):
        """Pauses Wi-Fi packet streaming to the strip for the duration so alert flashes are not overwritten."""
        self._pause_until = time.time() + duration

    def start(self, zone: Optional[str] = None):
        if zone:
            self.zone = zone
        if self.is_running:
            return

        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.is_running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

        # Restore user's manual brightness
        try:
            self.controller.set_brightness(self.controller.brightness, immediate=True)
        except Exception:
            pass

    def toggle(self) -> bool:
        if self.is_running:
            self.stop()
            return False
        else:
            self.start()
            return True

    def _get_capture_bounds(self) -> Tuple[range, range]:
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)

        if self.zone == "custom" and self.custom_rect:
            x1, y1, x2, y2 = self.custom_rect
            x_min, x_max = max(0, min(x1, x2)), min(sw, max(x1, x2))
            y_min, y_max = max(0, min(y1, y2)), min(sh, max(y1, y2))
            step_x = max(10, (x_max - x_min) // 10)
            step_y = max(10, (y_max - y_min) // 8)
            return range(x_min, x_max, step_x), range(y_min, y_max, step_y)

        elif self.zone == "top":
            step_x = max(20, sw // 14)
            step_y = max(15, (sh // 3) // 5)
            return range(sw // 10, sw * 9 // 10, step_x), range(sh // 25, sh // 3, step_y)

        elif self.zone == "bottom":
            step_x = max(20, sw // 14)
            step_y = max(15, (sh // 3) // 5)
            return range(sw // 10, sw * 9 // 10, step_x), range(sh * 65 // 100, sh * 95 // 100, step_y)

        elif self.zone == "center":
            step_x = max(20, sw // 12)
            step_y = max(15, sh // 10)
            return range(sw // 4, sw * 3 // 4, step_x), range(sh // 4, sh * 3 // 4, step_y)

        else:  # "whole"
            step_x = max(25, sw // 12)
            step_y = max(20, sh // 9)
            return range(sw // 12, sw * 11 // 12, step_x), range(sh // 10, sh * 9 // 10, step_y)

    def _sample_screen(self, hdc, x_range: range, y_range: range) -> Tuple[int, int, int, float, bool]:
        r_tot, g_tot, b_tot = 0, 0, 0
        count = 0
        black_count = 0
        white_pixel_count = 0
        saturated_count = 0

        hue_bins_r = [0] * 8
        hue_bins_g = [0] * 8
        hue_bins_b = [0] * 8
        hue_bins_count = [0] * 8

        for y in y_range:
            for x in x_range:
                pixel = gdi32.GetPixel(hdc, x, y)
                if pixel != -1:
                    r = pixel & 0xFF
                    g = (pixel >> 8) & 0xFF
                    b = (pixel >> 16) & 0xFF
                    count += 1
                    r_tot += r
                    g_tot += g
                    b_tot += b

                    p_max = max(r, g, b)
                    p_min = min(r, g, b)
                    delta = p_max - p_min

                    # Dark/black pixel threshold
                    if p_max < 28:
                        black_count += 1
                        continue

                    # Genuine white / grey
                    if delta < 20 and p_max > 100:
                        white_pixel_count += 1
                    elif delta >= 30:
                        saturated_count += 1
                        # Fast approximate hue binning
                        if p_max == r:
                            if g >= b:
                                bin_idx = 1 if (g - b) > (r - b) // 2 else 0
                            else:
                                bin_idx = 7 if (b - g) < (r - g) // 2 else 6
                        elif p_max == g:
                            bin_idx = 2 if r >= b else 3
                        else:  # p_max == b
                            bin_idx = 4 if r < g else 5

                        hue_bins_r[bin_idx] += r
                        hue_bins_g[bin_idx] += g
                        hue_bins_b[bin_idx] += b
                        hue_bins_count[bin_idx] += 1

        if count == 0:
            return 255, 215, 215, 0.0, False

        black_ratio = black_count / count
        colored_pixels = count - black_count

        is_clutter = False
        if colored_pixels > 0:
            max_bin_count = max(hue_bins_count)
            dominant_bin = hue_bins_count.index(max_bin_count)
            # Active bins with at least 12% of colored pixels
            active_bins = sum(1 for c in hue_bins_count if c >= max(3, int(colored_pixels * 0.12)))

            # If 3+ distinct competing colors and no single color is dominant (< 40%):
            if active_bins >= 3 and max_bin_count < int(colored_pixels * 0.40) and saturated_count > int(colored_pixels * 0.35):
                is_clutter = True
                out_r = r_tot // count
                out_g = g_tot // count
                out_b = b_tot // count
            elif max_bin_count >= max(3, int(colored_pixels * 0.30)):
                # Color simplification: snap cleanly to dominant color
                out_r = hue_bins_r[dominant_bin] // max_bin_count
                out_g = hue_bins_g[dominant_bin] // max_bin_count
                out_b = hue_bins_b[dominant_bin] // max_bin_count
            else:
                out_r = r_tot // count
                out_g = g_tot // count
                out_b = b_tot // count
        else:
            out_r = r_tot // count
            out_g = g_tot // count
            out_b = b_tot // count

        return out_r, out_g, out_b, black_ratio, is_clutter

    def _calculate_dynamic_brightness(self, raw_r: int, raw_g: int, raw_b: int, black_ratio: float, is_clutter: bool) -> float:
        # Rule 1: If > 70% of screen is black, drop brightness to 1%
        if black_ratio >= 0.70:
            return 1.0

        # Rule 2: If scene is multi-color clutter that strip cannot cleanly display, drop brightness to 1%
        if is_clutter:
            return 1.0

        # Rule 3: Natural dynamic brightness curve without artificial baseline
        v = max(raw_r, raw_g, raw_b) / 255.0
        lum = (0.299 * raw_r + 0.587 * raw_g + 0.114 * raw_b) / 255.0
        lightness = max(0.0, min(1.0, max(v, lum)))

        min_b = float(self.min_brightness)
        max_b = float(self.max_brightness)

        t = pow(lightness, self.gamma)
        target_b = min_b + (max_b - min_b) * t

        return max(1.0, min(max_b, target_b))

    def _process_color(self, r: int, g: int, b: int) -> Tuple[int, int, int]:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)

        # Genuine white: blend into calibrated white target (#FFD7D7)
        if s < 0.10:
            cal_r, cal_g, cal_b = self.calibrated_white
            weight = 1.0 - (s / 0.10)
            out_r = int((r * (1.0 - weight) + cal_r * weight))
            out_g = int((g * (1.0 - weight) + cal_g * weight))
            out_b = int((b * (1.0 - weight) + cal_b * weight))
            return max(0, min(255, out_r)), max(0, min(255, out_g)), max(0, min(255, out_b))

        s_boosted = min(1.0, s * self.saturation_boost)
        nr, ng, nb = colorsys.hsv_to_rgb(h, s_boosted, 1.0)
        return int(nr * 255), int(ng * 255), int(nb * 255)

    def _run_loop(self):
        if winmm:
            try:
                winmm.timeBeginPeriod(1)
            except Exception:
                pass

        try:
            self._curr_r, self._curr_g, self._curr_b = [float(x) for x in self.controller.color]
            self._curr_brightness = float(self.controller.brightness or 100)

            if not self.controller.power:
                self.controller.set_power(True)

            frame_count = 0
            fps_start = time.perf_counter()
            last_preview_time = 0.0

            acc_r = 0
            acc_g = 0
            acc_b = 0
            acc_black_ratio = 0.0
            acc_clutter_votes = 0
            acc_count = 0

            while not self._stop_event.is_set():
                loop_start = time.perf_counter()

                target_hz = self.monitor_hz if self.vsync else max(15, self.fps)
                target_interval = 1.0 / target_hz

                # Frame measurement
                frame_count += 1
                now_perf = time.perf_counter()
                dt = now_perf - fps_start
                if dt >= 0.5:
                    calc_fps = round(frame_count / dt, 0)
                    if calc_fps > 5:
                        self.measured_fps = calc_fps
                    frame_count = 0
                    fps_start = now_perf

                try:
                    hdc = user32.GetDC(0)
                    x_range, y_range = self._get_capture_bounds()
                    raw_r, raw_g, raw_b, black_ratio, is_clutter = self._sample_screen(hdc, x_range, y_range)
                    user32.ReleaseDC(0, hdc)

                    acc_r += raw_r
                    acc_g += raw_g
                    acc_b += raw_b
                    acc_black_ratio += black_ratio
                    acc_clutter_votes += (1 if is_clutter else 0)
                    acc_count += 1

                    now = time.time()
                    time_since_send = now - self._last_sent_time

                    if time_since_send >= self.min_send_interval and acc_count > 0:
                        avg_r = acc_r // acc_count
                        avg_g = acc_g // acc_count
                        avg_b = acc_b // acc_count
                        avg_black_ratio = acc_black_ratio / acc_count
                        is_avg_clutter = (acc_clutter_votes / acc_count) >= 0.50
                        self.supersample_count = acc_count
                        self.last_black_ratio = avg_black_ratio
                        self.last_is_clutter = is_avg_clutter

                        acc_r = 0
                        acc_g = 0
                        acc_b = 0
                        acc_black_ratio = 0.0
                        acc_clutter_votes = 0
                        acc_count = 0

                        target_r, target_g, target_b = self._process_color(avg_r, avg_g, avg_b)
                        
                        # NEW: Simplification logic
                        if self.simplify_colors:
                            h, s, v = colorsys.rgb_to_hsv(target_r/255.0, target_g/255.0, target_b/255.0)
                            # snap to 8 main hues
                            hue_sector = round(h * 8) % 8
                            r, g, b = colorsys.hsv_to_rgb(hue_sector / 8.0, 1.0, 1.0)
                            target_r, target_g, target_b = int(r*255), int(g*255), int(b*255)

                        target_bright = self._calculate_dynamic_brightness(avg_r, avg_g, avg_b, avg_black_ratio, is_avg_clutter)

                        alpha = self.smoothing
                        self._curr_r = self._curr_r * (1.0 - alpha) + target_r * alpha
                        self._curr_g = self._curr_g * (1.0 - alpha) + target_g * alpha
                        self._curr_b = self._curr_b * (1.0 - alpha) + target_b * alpha
                        self._curr_brightness = self._curr_brightness * (1.0 - alpha) + target_bright * alpha

                        final_r = int(max(0, min(255, round(self._curr_r))))
                        final_g = int(max(0, min(255, round(self._curr_g))))
                        final_b = int(max(0, min(255, round(self._curr_b))))
                        final_bright = int(max(1, min(self.max_brightness, round(self._curr_brightness))))

                        dr = abs(final_r - self._last_sent_rgb[0])
                        dg = abs(final_g - self._last_sent_rgb[1])
                        db = abs(final_b - self._last_sent_rgb[2])
                        dbright = abs(final_bright - self._last_sent_brightness)

                        if time.time() >= self._pause_until and self.controller.power:
                            if (dr + dg + db >= 4 or dbright >= 2) or (time_since_send > 0.5):
                                self._last_sent_rgb = (final_r, final_g, final_b)
                                self._last_sent_brightness = final_bright
                                self._last_sent_time = now
                                self.controller.set_color(final_r, final_g, final_b, brightness=final_bright, immediate=False)

                    if self.on_preview_update and (now - last_preview_time >= 0.033):
                        last_preview_time = now
                        final_r = int(max(0, min(255, round(self._curr_r))))
                        final_g = int(max(0, min(255, round(self._curr_g))))
                        final_b = int(max(0, min(255, round(self._curr_b))))
                        final_bright = int(max(1, min(self.max_brightness, round(self._curr_brightness))))
                        try:
                            self.on_preview_update((final_r, final_g, final_b), final_bright, self.measured_fps, self.supersample_count, self.last_black_ratio, self.last_is_clutter)
                        except TypeError:
                            try:
                                self.on_preview_update((final_r, final_g, final_b), final_bright, self.measured_fps, self.supersample_count)
                            except TypeError:
                                self.on_preview_update((final_r, final_g, final_b))

                except Exception:
                    time.sleep(0.005)

                elapsed = time.perf_counter() - loop_start
                sleep_rem = target_interval - elapsed
                if sleep_rem > 0.0015:
                    time.sleep(sleep_rem - 0.0005)

        finally:
            if winmm:
                try:
                    winmm.timeEndPeriod(1)
                except Exception:
                    pass
