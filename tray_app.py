"""
System Tray Integration for Windows Taskbar Notification Area.
Uses pystray to provide a tray icon with live color glow, quick power toggle,
preset colors, brightness presets, and quick access to the main window.
"""

from typing import Callable, Optional
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu


def generate_tray_icon(rgb_color=(0, 200, 255), is_on=True, is_connected=True) -> Image.Image:
    """Draws a crisp, modern LED strip/bulb icon reflecting power and active color."""
    size = (64, 64)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    r, g, b = rgb_color

    if is_on:
        # Outer soft glow
        draw.ellipse([2, 2, 62, 62], fill=(r, g, b, 50))
        # Outer rim
        draw.ellipse([6, 6, 58, 58], fill=(25, 27, 32, 240), outline=(r, g, b, 230), width=3)
        # Inner glowing core
        draw.ellipse([16, 16, 48, 48], fill=(r, g, b, 255))
        # Center highlight
        draw.ellipse([24, 24, 40, 40], fill=(255, 255, 255, 190))
    else:
        # Off state
        draw.ellipse([6, 6, 58, 58], fill=(20, 20, 24, 240), outline=(90, 92, 100, 220), width=3)
        draw.ellipse([18, 18, 46, 46], fill=(55, 57, 65, 255))
        draw.ellipse([26, 26, 38, 38], fill=(40, 42, 48, 255))

    # Small status pip in the bottom right corner
    if not is_connected:
        # Small orange/red indicator
        draw.ellipse([46, 46, 60, 60], fill=(240, 80, 80, 255), outline=(20, 20, 20, 255), width=2)

    return img


class TrayApp:
    def __init__(
        self,
        on_toggle_window: Callable[[], None],
        on_toggle_power: Callable[[], None],
        on_set_brightness: Callable[[int], None],
        on_set_color: Callable[[int, int, int], None],
        on_quit: Callable[[], None],
        on_eyedropper: Optional[Callable[[], None]] = None,
        on_toggle_ambient: Optional[Callable[[], None]] = None,
    ):
        self.on_toggle_window = on_toggle_window
        self.on_toggle_power = on_toggle_power
        self.on_set_brightness = on_set_brightness
        self.on_set_color = on_set_color
        self.on_quit = on_quit
        self.on_eyedropper = on_eyedropper
        self.on_toggle_ambient = on_toggle_ambient

        self.power = True
        self.color = (0, 200, 255)
        self.is_connected = False
        self.is_ambient_active = False
        self.device_name = "flowerRGB"

        self.icon: Optional[pystray.Icon] = None

    def build_menu(self) -> Menu:
        power_title = "💡 Выключить ленту" if self.power else "💡 Включить ленту"

        # Preset colors for tray submenu
        color_menu = Menu(
            item("⚪ Дневной белый", lambda: self.on_set_color(255, 215, 215)),
            item("🟡 Тёплый свет", lambda: self.on_set_color(255, 180, 80)),
            item("🔴 Красный", lambda: self.on_set_color(255, 30, 30)),
            item("🟢 Зелёный", lambda: self.on_set_color(30, 230, 40)),
            item("🔵 Синий", lambda: self.on_set_color(30, 100, 255)),
            item("🟣 Неоновый фиолетовый", lambda: self.on_set_color(180, 40, 255)),
            item("🟠 Закат / Оранжевый", lambda: self.on_set_color(255, 110, 20)),
            item("🌸 Розовый / Magenta", lambda: self.on_set_color(255, 40, 150)),
            item("🌊 Морская волна / Cyan", lambda: self.on_set_color(0, 230, 230)),
        )

        brightness_menu = Menu(
            item("15% (Ночник)", lambda: self.on_set_brightness(15)),
            item("30% (Приглушённый)", lambda: self.on_set_brightness(30)),
            item("50% (Средний)", lambda: self.on_set_brightness(50)),
            item("75% (Яркий)", lambda: self.on_set_brightness(75)),
            item("100% (Максимум)", lambda: self.on_set_brightness(100)),
        )

        menu_items = [
            item("🖥️ Панель управления", lambda: self.on_toggle_window(), default=True),
            Menu.SEPARATOR,
            item(power_title, lambda: self.on_toggle_power()),
            item("🎨 Быстрый цвет", color_menu),
            item("🔆 Яркость", brightness_menu),
        ]

        if self.on_toggle_ambient:
            amb_title = "📺 Захват экрана (Ambilight): [ВКЛ]" if self.is_ambient_active else "📺 Захват экрана (Ambilight): [ВЫКЛ]"
            menu_items.append(item(amb_title, lambda: self.on_toggle_ambient()))

        if self.on_eyedropper:
            menu_items.append(item("💉 Пипетка (цвет с экрана)", lambda: self.on_eyedropper()))

        menu_items.extend([
            Menu.SEPARATOR,
            item("❌ Выход", lambda: self.on_quit()),
        ])

        return Menu(*menu_items)

    def start(self, device_name="flowerRGB", initial_power=True, initial_color=(0, 200, 255), is_connected=False):
        self.device_name = device_name
        self.power = initial_power
        self.color = initial_color
        self.is_connected = is_connected

        initial_img = generate_tray_icon(self.color, self.power, self.is_connected)
        self.icon = pystray.Icon(
            name="flowerRGB",
            icon=initial_img,
            title=f"{self.device_name} ({'ВКЛ' if self.power else 'ВЫКЛ'})",
            menu=self.build_menu()
        )
        self.icon.run_detached()

    def update_state(self, power: bool, color: tuple, is_connected: bool, device_name: Optional[str] = None):
        """Updates the tray icon image, tooltip and context menu to match device state."""
        self.power = power
        self.color = color
        self.is_connected = is_connected
        if device_name:
            self.device_name = device_name

        if self.icon:
            try:
                self.icon.icon = generate_tray_icon(self.color, self.power, self.is_connected)
                state_str = "ВКЛ" if self.power else "ВЫКЛ"
                conn_str = "" if self.is_connected else " (нет связи)"
                self.icon.title = f"{self.device_name}: {state_str}{conn_str}"
                self.icon.menu = self.build_menu()
            except Exception as e:
                print(f"[Tray] Error updating tray: {e}")

    def set_ambient_state(self, is_active: bool):
        self.is_ambient_active = is_active
        if self.icon:
            try:
                self.icon.menu = self.build_menu()
            except Exception:
                pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
