import argparse
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import hid
else:
    import hidraw as hid

CONSOLE_USAGE_PAGE = 0xFF31
CONSOLE_USAGE = 0x0074

SCAN_INTERVAL = 2
READ_TIMEOUT_MS = 500

APP_NAME = "QMK Layer Notifier"

OSD_FONT = "Segoe UI"
OSD_BACKGROUND = "#1e1e2e"
OSD_BORDER = "#45475a"
OSD_FOREGROUND = "#cdd6f4"
OSD_MUTED = "#a6adc8"
OSD_ALPHA = 0.96
OSD_MARGIN = 64
OSD_DISPLAY_MS = 1600
OSD_FADE_MS = 16
OSD_FADE_STEP = 0.12
OSD_POLL_MS = 25

LAYER_PATTERN = re.compile(
    r"QMK_LAYER:(mouse|keyboard):([^:\r\n]+):(\d+)"
)
SCROLL_PATTERN = re.compile(
    r"QMK_SCROLL:(mouse|keyboard):([^:\r\n]+):([01])$"
)

OUTPUT_SWAYNC = "swaync"
OUTPUT_SWAYOSD = "swayosd"
OUTPUT_CHOICES = (OUTPUT_SWAYNC, OUTPUT_SWAYOSD)
DEFAULT_OUTPUT = OUTPUT_SWAYNC
SCROLL_TIMEOUT_MS = 2147483647
TRANSIENT_TIMEOUT_MS = 2000

_output_backend = DEFAULT_OUTPUT
_backend_lock = threading.Lock()
_shutdown_event = threading.Event()
_fatal_backend_error = False
_scroll_lock = threading.Lock()
_scroll_notifications = {}
_layer_lock = threading.Lock()
_layer_notifications = {}


def resource_path(relative):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def _latest_pending(items):
    latest = None
    stop = False

    while True:
        try:
            item = items.get_nowait()
        except queue.Empty:
            break

        if item is None:
            stop = True
            break

        latest = item

    return latest, stop


def _enable_dpi_awareness():
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _work_area():
    import ctypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    user32 = ctypes.windll.user32
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_ulong]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(MONITORINFO),
    ]

    point = POINT()

    if not user32.GetCursorPos(ctypes.byref(point)):
        return None

    monitor = user32.MonitorFromPoint(point, 2)

    if not monitor:
        return None

    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)

    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None

    return (
        info.rcWork.left,
        info.rcWork.top,
        info.rcWork.right,
        info.rcWork.bottom,
    )


def _make_click_through(hwnd):
    import ctypes

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000

    user32 = ctypes.windll.user32
    user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_long,
    ]
    user32.SetWindowLongW.restype = ctypes.c_long

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd,
        GWL_EXSTYLE,
        style
        | WS_EX_LAYERED
        | WS_EX_TRANSPARENT
        | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE,
    )


class WindowsOSD:
    """Draw a lightweight always-on-top overlay from a dedicated Tk thread."""

    def __init__(self):
        self._queue = queue.Queue()
        self._enabled = True
        self._thread = threading.Thread(
            target=self._run, name="qmk-osd", daemon=True
        )
        self._thread.start()

    def notify(self, title, body, device_type):
        if self._enabled:
            self._queue.put((title, body, device_type))

    def stop(self):
        self._enabled = False
        self._queue.put(None)

        if self._thread.is_alive():
            self._thread.join()

    def _run(self):
        try:
            import tkinter as tk
        except Exception as exc:
            print(f"OSD disabled: cannot import tkinter ({exc})", flush=True)
            self._enabled = False
            return

        _enable_dpi_awareness()

        try:
            root = tk.Tk()
        except Exception as exc:
            print(f"OSD disabled: cannot create window ({exc})", flush=True)
            self._enabled = False
            return

        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        root.configure(bg=OSD_BACKGROUND)

        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        icon_size = int(40 * scale)

        panel = tk.Frame(
            root,
            bg=OSD_BACKGROUND,
            padx=18,
            pady=12,
            highlightthickness=1,
            highlightbackground=OSD_BORDER,
        )
        panel.pack()

        icon = tk.Label(panel, bg=OSD_BACKGROUND)
        icon.pack(side="left", padx=(0, 14))

        text = tk.Frame(panel, bg=OSD_BACKGROUND)
        text.pack(side="left")

        title_label = tk.Label(
            text,
            bg=OSD_BACKGROUND,
            fg=OSD_FOREGROUND,
            font=(OSD_FONT, 12, "bold"),
            anchor="w",
            justify="left",
        )
        title_label.pack(anchor="w")

        body_label = tk.Label(
            text,
            bg=OSD_BACKGROUND,
            fg=OSD_MUTED,
            font=(OSD_FONT, 10),
            anchor="w",
            justify="left",
        )
        body_label.pack(anchor="w")

        root.update_idletasks()

        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = user32.GetAncestor(root.winfo_id(), 2)
            _make_click_through(hwnd)
        except Exception as exc:
            print(f"OSD window styles unavailable ({exc})", flush=True)

        images = {}

        for device_type in ("keyboard", "mouse"):
            path = resource_path(f"assets/{device_type}.png")

            if not path.is_file():
                continue

            try:
                image = tk.PhotoImage(file=str(path))
                factor = max(1, image.width() // icon_size)
                images[device_type] = image.subsample(factor)
            except Exception as exc:
                print(f"OSD icon error ({path}): {exc}", flush=True)

        timers = {"fade": None, "hide": None}

        def cancel(name):
            job = timers[name]

            if job is not None:
                root.after_cancel(job)
                timers[name] = None

        def fade(target):
            cancel("fade")
            current = float(root.attributes("-alpha"))

            def step():
                nonlocal current

                if current < target:
                    current = min(target, current + OSD_FADE_STEP)
                else:
                    current = max(target, current - OSD_FADE_STEP)

                root.attributes("-alpha", current)

                if abs(current - target) > 0.001:
                    timers["fade"] = root.after(OSD_FADE_MS, step)
                else:
                    timers["fade"] = None

                    if target == 0.0:
                        root.withdraw()

            step()

        def place():
            root.update_idletasks()
            width = panel.winfo_reqwidth()
            height = panel.winfo_reqheight()
            area = _work_area()

            if area is None:
                x = (root.winfo_screenwidth() - width) // 2
                y = root.winfo_screenheight() - height - OSD_MARGIN
            else:
                left, top, right, bottom = area
                x = left + (right - left - width) // 2
                y = bottom - height - OSD_MARGIN

            root.geometry(
                f"{width}x{height}+{max(x, 0)}+{max(y, 0)}"
            )

        def show(title, body, device_type):
            title_label.configure(text=title)
            body_label.configure(text=body)
            icon.configure(image=images.get(device_type, ""))
            place()
            cancel("hide")

            if root.state() == "withdrawn":
                root.attributes("-alpha", 0.0)
                root.deiconify()

            fade(OSD_ALPHA)
            timers["hide"] = root.after(OSD_DISPLAY_MS, hide)

        def hide():
            timers["hide"] = None
            fade(0.0)

        def poll():
            latest, stop = _latest_pending(self._queue)

            if latest is not None:
                show(*latest)

            if stop:
                root.destroy()
                return

            root.after(OSD_POLL_MS, poll)

        root.after(0, poll)

        try:
            root.mainloop()
        except Exception as exc:
            print(f"OSD stopped: {exc}", flush=True)
        finally:
            self._enabled = False

            try:
                root.destroy()
            except Exception:
                pass


_windows_osd = None
_windows_osd_lock = threading.Lock()


def _get_windows_osd():
    global _windows_osd

    with _windows_osd_lock:
        if _windows_osd is None:
            _windows_osd = WindowsOSD()

        return _windows_osd


def shutdown_notifications():
    _close_all_scroll_notifications()
    _close_all_layer_notifications()

    global _windows_osd

    with _windows_osd_lock:
        osd = _windows_osd
        _windows_osd = None

    if osd is not None:
        osd.stop()


def find_devices():
    """Discover all QMK console HID interfaces."""
    devices = {}

    for device in hid.enumerate():
        if (
            device["usage_page"] == CONSOLE_USAGE_PAGE
            and device["usage"] == CONSOLE_USAGE
        ):
            # Dictionary avoids duplicate paths.
            devices[device["path"]] = device

    return devices


def _command_exists(name):
    return shutil.which(name) is not None


def _dbus_command():
    if _command_exists("gdbus"):
        return "gdbus"

    if _command_exists("dbus-send"):
        return "dbus-send"

    return None


def _swaync_available():
    return (
        _command_exists("notify-send")
        and _dbus_command() is not None
    )


def _swayosd_available():
    return _command_exists("swayosd-client")


def _available_backends():
    backends = []

    if _swaync_available():
        backends.append(OUTPUT_SWAYNC)

    if _swayosd_available():
        backends.append(OUTPUT_SWAYOSD)

    return backends


def _resolve_output_backend(requested):
    available = _available_backends()

    if not available:
        return None

    if requested in available:
        return requested

    fallback = (
        OUTPUT_SWAYOSD
        if requested == OUTPUT_SWAYNC
        else OUTPUT_SWAYNC
    )

    if fallback in available:
        print(
            f"Warning: output backend {requested!r} is unavailable; "
            f"falling back to {fallback!r}.",
            file=sys.stderr,
            flush=True,
        )
        return fallback

    return available[0]


def _set_output_backend(backend):
    global _output_backend

    with _backend_lock:
        _output_backend = backend


def _current_backend():
    with _backend_lock:
        return _output_backend


def _fatal_backend_failure():
    global _fatal_backend_error

    with _backend_lock:
        if _fatal_backend_error:
            return

        _fatal_backend_error = True

    print(
        "Error: no notification backend available; shutting down.",
        file=sys.stderr,
        flush=True,
    )
    _shutdown_event.set()


def _try_notify_osd(title, body, device_type):
    try:
        notify_osd(title, body, device_type)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Notification error: {exc}", file=sys.stderr, flush=True)
        _fatal_backend_failure()
        return False

    return True


def _notify_transient_swaync(title, body, device_type):
    command = [
        "notify-send",
        "-a", APP_NAME,
        "-u", "normal",
        "-i", f"input-{device_type}",
        "-t", str(TRANSIENT_TIMEOUT_MS),
        title,
        body,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Notification error: {exc}", file=sys.stderr, flush=True)
        _fallback_to_swayosd(title, body, device_type)
        return False

    if result.returncode != 0:
        print(
            "Notification error: notify-send exited with "
            f"{result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
            flush=True,
        )
        _fallback_to_swayosd(title, body, device_type)
        return False

    return True


def _send_persistent_notification(
    title, body, device_type, replaces_id=None
):
    command = [
        "notify-send",
        "-a", APP_NAME,
        "-u", "normal",
        "-i", f"input-{device_type}",
        "-t", str(SCROLL_TIMEOUT_MS),
        "-p",
    ]

    if replaces_id is not None:
        command.extend(["-r", str(replaces_id)])

    command.extend([title, body])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Notification error: {exc}", file=sys.stderr, flush=True)
        return None

    if result.returncode != 0:
        print(
            "Notification error: notify-send exited with "
            f"{result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
            flush=True,
        )
        return None

    output = result.stdout.strip()

    if not output:
        print(
            "Notification error: notify-send did not return an ID.",
            file=sys.stderr,
            flush=True,
        )
        return None

    try:
        return int(output.splitlines()[0])
    except ValueError:
        print(
            f"Notification error: invalid notify-send output: {output!r}",
            file=sys.stderr,
            flush=True,
        )
        return None


def _close_notification(notification_id):
    if notification_id is None:
        return False

    if _command_exists("gdbus"):
        command = [
            "gdbus", "call",
            "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method",
            "org.freedesktop.Notifications.CloseNotification",
            str(notification_id),
        ]
    elif _command_exists("dbus-send"):
        command = [
            "dbus-send",
            "--session",
            "--type=method_call",
            "--dest=org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications.CloseNotification",
            f"uint32:{notification_id}",
        ]
    else:
        print(
            "Notification error: no D-Bus command available to close "
            "notifications.",
            file=sys.stderr,
            flush=True,
        )
        return False

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Notification error: {exc}", file=sys.stderr, flush=True)
        return False

    if result.returncode != 0:
        print(
            "Notification error: cannot close notification "
            f"{notification_id}: {result.stderr.strip()}",
            file=sys.stderr,
            flush=True,
        )
        return False

    return True


def _fallback_to_swayosd(title, body, device_type):
    global _output_backend

    with _backend_lock:
        already = _output_backend == OUTPUT_SWAYOSD
        _output_backend = OUTPUT_SWAYOSD

    if not already:
        print(
            "Warning: SwayNC notification failed; falling back to SwayOSD.",
            file=sys.stderr,
            flush=True,
        )
        _close_all_scroll_notifications()
        _close_all_layer_notifications()

    return _try_notify_osd(title, body, device_type)


def _scroll_key(device_type, device_name, source=None):
    if source is not None:
        return source

    return (device_type, device_name)


def _notify_scroll_swaync(device_type, device_name, enabled, source=None):
    key = _scroll_key(device_type, device_name, source)

    if enabled:
        with _scroll_lock:
            existing = _scroll_notifications.get(key)

        replaces_id = existing if isinstance(existing, int) else None
        notification_id = _send_persistent_notification(
            device_name,
            "scroll mode on",
            device_type,
            replaces_id=replaces_id,
        )

        if notification_id is None:
            if replaces_id is not None:
                _close_notification(replaces_id)

            with _scroll_lock:
                _scroll_notifications[key] = None

            _fallback_to_swayosd(
                device_name, "scroll mode on", device_type
            )
            return

        with _scroll_lock:
            _scroll_notifications[key] = notification_id

        return

    with _scroll_lock:
        notification_id = _scroll_notifications.pop(key, None)

    if notification_id is not None:
        _close_notification(notification_id)

    _notify_transient_swaync(device_name, "scroll mode off", device_type)


def _notify_scroll_swayosd(device_type, device_name, enabled, source=None):
    key = _scroll_key(device_type, device_name, source)

    with _scroll_lock:
        notification_id = _scroll_notifications.pop(key, None)

    if notification_id is not None:
        _close_notification(notification_id)

    state = "on" if enabled else "off"
    _try_notify_osd(device_name, f"scroll mode {state}", device_type)


def _cleanup_scroll_source(source):
    if source is None:
        return

    with _scroll_lock:
        notification_id = _scroll_notifications.pop(source, None)

    if notification_id is not None:
        _close_notification(notification_id)


def _close_all_scroll_notifications():
    with _scroll_lock:
        notification_ids = [
            notification_id
            for notification_id in _scroll_notifications.values()
            if notification_id is not None
        ]
        _scroll_notifications.clear()

    for notification_id in notification_ids:
        _close_notification(notification_id)


def _layer_key(device_type, device_name, source=None):
    return _scroll_key(device_type, device_name, source)


def _notify_layer_swaync(device_type, device_name, layer, source=None):
    key = _layer_key(device_type, device_name, source)
    layer = int(layer)

    if layer > 0:
        with _layer_lock:
            existing = _layer_notifications.get(key)

        replaces_id = existing if isinstance(existing, int) else None
        notification_id = _send_persistent_notification(
            device_name,
            f"layer {layer}",
            device_type,
            replaces_id=replaces_id,
        )

        if notification_id is None:
            if replaces_id is not None:
                _close_notification(replaces_id)

            with _layer_lock:
                _layer_notifications[key] = None

            _fallback_to_swayosd(
                device_name, f"layer {layer}", device_type
            )
            return

        with _layer_lock:
            _layer_notifications[key] = notification_id

        return

    with _layer_lock:
        notification_id = _layer_notifications.pop(key, None)

    if notification_id is not None:
        _close_notification(notification_id)

    _notify_transient_swaync(device_name, "layer 0", device_type)


def _notify_layer_swayosd(device_type, device_name, layer, source=None):
    key = _layer_key(device_type, device_name, source)

    with _layer_lock:
        notification_id = _layer_notifications.pop(key, None)

    if notification_id is not None:
        _close_notification(notification_id)

    _try_notify_osd(device_name, f"layer {int(layer)}", device_type)


def _cleanup_layer_source(source):
    if source is None:
        return

    with _layer_lock:
        notification_id = _layer_notifications.pop(source, None)

    if notification_id is not None:
        _close_notification(notification_id)


def _close_all_layer_notifications():
    with _layer_lock:
        notification_ids = [
            notification_id
            for notification_id in _layer_notifications.values()
            if notification_id is not None
        ]
        _layer_notifications.clear()

    for notification_id in notification_ids:
        _close_notification(notification_id)


def notify_layer(device_type, device_name, layer, source=None):
    if IS_WINDOWS:
        _get_windows_osd().notify(
            device_name, f"layer {int(layer)}", device_type
        )
        return

    if _current_backend() == OUTPUT_SWAYNC:
        _notify_layer_swaync(
            device_type, device_name, layer, source
        )
    else:
        _notify_layer_swayosd(
            device_type, device_name, layer, source
        )


def notify_scroll(device_type, device_name, enabled, source=None):
    if IS_WINDOWS:
        state = "on" if enabled else "off"
        _get_windows_osd().notify(
            device_name, f"scroll mode {state}", device_type
        )
        return

    if _current_backend() == OUTPUT_SWAYNC:
        _notify_scroll_swaync(
            device_type, device_name, enabled, source
        )
    else:
        _notify_scroll_swayosd(
            device_type, device_name, enabled, source
        )


def notify_osd(title, body, device_type):
    subprocess.run(
        [
            "swayosd-client",
            "--custom-message", f"{title} · {body}",
            "--custom-icon", f"input-{device_type}",
        ],
        check=False,
    )


def notify(title, body, device_type):
    if IS_WINDOWS:
        _get_windows_osd().notify(title, body, device_type)
    elif _current_backend() == OUTPUT_SWAYNC:
        _notify_transient_swaync(title, body, device_type)
    else:
        _try_notify_osd(title, body, device_type)


def process_line(line, source=None):
    match = LAYER_PATTERN.search(line)

    if match:
        device_type, device_name, layer = match.groups()
        body = f"layer {int(layer)}"

        print(f"{device_name}: {body}", flush=True)
        notify_layer(
            device_type, device_name, int(layer), source=source
        )
        return

    match = SCROLL_PATTERN.search(line)
    if match:
        device_type, device_name, enabled = match.groups()
        state = "on" if enabled == "1" else "off"
        body = f"scroll mode {state}"

        print(f"{device_name}: {body}", flush=True)
        notify_scroll(
            device_type, device_name, enabled == "1", source=source
        )


def monitor(path, info, stop_event):
    """Monitor one QMK console interface."""
    device = hid.device()

    try:
        device.open_path(path)

        name = info.get("product_string") or str(path)
        print(f"Connected: {name}", flush=True)

        buffer = ""

        while not stop_event.is_set():
            packet = device.read(
                32, timeout_ms=READ_TIMEOUT_MS
            )

            if not packet:
                continue

            chunk = bytes(packet).replace(b"\x00", b"")
            buffer += chunk.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                process_line(line.strip(), source=path)

            if len(buffer) > 4096:
                buffer = buffer[-1024:]

    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Device error ({path!r}): {exc}", flush=True)

    finally:
        try:
            _cleanup_scroll_source(path)
        finally:
            try:
                _cleanup_layer_source(path)
            finally:
                device.close()
                print(f"Monitor stopped: {path!r}", flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Show QMK layer and drag-scroll changes as "
            "desktop notifications."
        )
    )
    parser.add_argument(
        "--output",
        choices=OUTPUT_CHOICES,
        default=DEFAULT_OUTPUT,
        help=(
            "Notification backend to use on Linux "
            "(default: %(default)s)."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    global _fatal_backend_error

    _shutdown_event.clear()
    _fatal_backend_error = False

    args = parse_args(argv)

    if not IS_WINDOWS:
        backend = _resolve_output_backend(args.output)

        if backend is None:
            print(
                "Error: no notification backend found. Install SwayNC "
                "with notify-send and gdbus (or dbus-send), or install "
                "SwayOSD.",
                file=sys.stderr,
                flush=True,
            )
            return 1

        _set_output_backend(backend)

    # path -> (thread, stop_event)
    monitors = {}

    try:
        while not _shutdown_event.is_set():
            devices = find_devices()

            # Stop monitors for disconnected devices
            for path in list(monitors):
                thread, stop_event = monitors[path]

                if path not in devices or not thread.is_alive():
                    stop_event.set()
                    thread.join()
                    del monitors[path]

            # Start monitors for new devices
            for path, info in devices.items():
                if path in monitors:
                    continue

                stop_event = threading.Event()

                thread = threading.Thread(
                    target=monitor,
                    args=(path, info, stop_event),
                    daemon=True,
                )

                monitors[path] = (thread, stop_event)
                thread.start()

            _shutdown_event.wait(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping QMK monitor...", flush=True)

    finally:
        for thread, stop_event in monitors.values():
            stop_event.set()

        for thread, _ in monitors.values():
            thread.join()

        shutdown_notifications()

    return 1 if _fatal_backend_error else 0


if __name__ == "__main__":
    sys.exit(main())
