"""
Smart Life / Tuya RGB Strip Windows Tray & Desktop Controller.
Entry point for launching the application.
"""

import sys
import os

from config import load_config, save_config
from tuya_controller import TuyaController
from tray_app import TrayApp
from gui import MainWindow


def main():
    # Single-instance mutex check to prevent multiple instances
    if sys.platform == "win32":
        try:
            import ctypes
            ERROR_ALREADY_EXISTS = 183
            kernel32 = ctypes.windll.kernel32
            global _app_mutex
            _app_mutex = kernel32.CreateMutexW(None, False, "Local\\FlowerRGBControllerSingleInstanceMutex")
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                print("[Main] Another instance is already running. Exiting.")
                sys.exit(0)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FlowerRGB.Controller.1.0")
        except Exception:
            pass

    # 1. Load Configuration
    config = load_config()

    # Determine if should start minimized
    start_minimized = "--minimized" in sys.argv or config.get("start_minimized", False)

    # 2. Instantiate Controller
    controller = TuyaController(config=config)

    # Boot Sequence: turn strip ON with calibrated white (#FFD7D7) at 100% on PC startup
    controller.boot_sequence()

    # Shutdown Hooks: turn strip OFF when PC shuts down, restarts, or logs off
    def on_pc_shutdown():
        if controller.config.get("turn_off_on_pc_shutdown", True):
            controller.turn_off_immediate()

    import atexit
    atexit.register(on_pc_shutdown)

    if sys.platform == "win32":
        try:
            import ctypes
            HANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
            def _console_ctrl_handler(ctrl_type):
                if ctrl_type in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                    on_pc_shutdown()
                    return True
                return False
            global _ctrl_handler_ref
            _ctrl_handler_ref = HANDLER_ROUTINE(_console_ctrl_handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler_ref, True)
        except Exception:
            pass

    # 3. Create Main Window
    gui = MainWindow(controller=controller, start_minimized=start_minimized)

    # 4. Instantiate and configure System Tray App
    def on_toggle_window():
        gui.after(0, gui.toggle_window)

    def on_toggle_power():
        gui.after(0, gui._on_power_toggle_clicked)

    def on_set_brightness(val: int):
        gui.after(0, lambda: gui._set_brightness_preset(val))

    def on_set_color(r: int, g: int, b: int):
        gui.after(0, lambda: gui._set_color_preset((r, g, b)))

    def on_eyedropper():
        gui.after(0, gui.start_eyedropper)

    def on_toggle_ambient():
        gui.after(0, gui._on_toggle_ambient_clicked)

    def on_quit():
        """Clean shutdown that terminates all background threads and releases mutex."""
        def do_shutdown():
            try:
                gui.ambient_engine.stop()
                gui.chroma_client.disconnect()
                gui.homekit_bridge.stop()
                if hasattr(gui, "idle_monitor"):
                    gui.idle_monitor.stop()
                if hasattr(gui, "schedule_tracker"):
                    gui.schedule_tracker.stop()
                tray.stop()
                controller.stop()
                save_config(controller.config)
                gui.destroy()
            except Exception:
                pass
            finally:
                os._exit(0)

        gui.after(0, do_shutdown)

    def on_window_close():
        # If user explicitly configured minimize_to_tray, hide; otherwise cleanly quit
        if config.get("minimize_to_tray", False):
            gui.hide_to_tray()
            return False
        else:
            on_quit()
            return True

    gui.on_close_callback = on_window_close

    tray = TrayApp(
        on_toggle_window=on_toggle_window,
        on_toggle_power=on_toggle_power,
        on_set_brightness=on_set_brightness,
        on_set_color=on_set_color,
        on_quit=on_quit,
        on_eyedropper=on_eyedropper,
        on_toggle_ambient=on_toggle_ambient,
    )

    # Connect GUI callbacks
    gui.on_tray_update = tray.update_state
    gui.on_tray_ambient_update = tray.set_ambient_state

    # Connect Controller callback to GUI (which then also updates Tray)
    controller.on_state_change = gui.on_controller_state_changed

    # 5. Start Tray Icon
    tray.start(
        device_name=config.get("device_name", "flowerRGB"),
        initial_power=controller.power,
        initial_color=controller.color,
        is_connected=controller.is_connected
    )

    # 6. Window visibility is handled at creation via hidden=start_minimized

    # 7. Start Tkinter Main Loop
    try:
        gui.mainloop()
    except (KeyboardInterrupt, SystemExit):
        tray.stop()
        controller.stop()


if __name__ == "__main__":
    main()
