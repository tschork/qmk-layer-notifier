# QMK Layer Notifier

A small Linux and Windows 10/11 daemon that watches QMK USB console messages and
shows layer and drag-scroll mode changes through desktop notifications. On Linux
it uses SwayNotificationCenter (SwayNC) by default and SwayOSD as a fallback; on
Windows it draws its own lightweight always-on-top overlay. It discovers
compatible devices automatically, monitors multiple devices, and reconnects when
they are unplugged and plugged back in.

The daemon reads the QMK **console HID interface** (usage page `0xFF31`, usage
`0x74`). It does **not** use QMK Raw HID (`RAW_ENABLE`). When a matching message
arrives, it prints the layer to stdout and displays an on-screen indicator.

## Host requirements

### Linux
- QMK tools installed and udev rules deployed (utils/install_udev.sh in official qmk repo)
- Access to the keyboard's `/dev/hidraw*` console interface.
- A notification backend in `PATH` and running in the graphical session:
  - **SwayNC** (default): `notify-send` plus `gdbus` or `dbus-send`, and a
    notification daemon on the session bus (`org.freedesktop.Notifications`).
  - **SwayOSD** (fallback): `swayosd-client`, with the SwayOSD server running.
- For running from source: Python 3 and the Python package `hidapi` (imported
  by the program as `hidraw`).

Run from source:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install hidapi
.venv/bin/python main.py
```

Select a backend explicitly with `--output`:

```sh
.venv/bin/python main.py --output swaync
.venv/bin/python main.py --output swayosd
```

To build a standalone executable with PyInstaller:

```sh
.venv/bin/python -m pip install pyinstaller
.venv/bin/python -m PyInstaller --onefile --name qmk-layer-notifier main.py
./dist/qmk-layer-notifier
```

The executable still needs `notify-send`/`gdbus` or `swayosd-client` and access
to the HID device; bundling Python does not bundle those host services. Stop the
daemon with Ctrl+C.

#### Linux notification backends

The daemon picks the backend from `--output`:

- `swaync` (default): sends notifications through the freedesktop notification
  service using `notify-send`. Layer changes above layer 0 stay visible until
  the layer changes again or the firmware reports layer 0; layer 0 closes the
  persistent layer notification and shows a short-lived "layer 0" message.
  Drag-scroll "on" stays visible until the device reports "off", the device
  disconnects, or the daemon stops. The daemon tracks notification IDs and
  closes them explicitly. This needs `notify-send` and `gdbus` (or
  `dbus-send`).
- `swayosd`: sends every message through `swayosd-client`. SwayOSD indicators
  are transient, so layer and drag-scroll "on" indicators are not kept
  visible.

If the requested backend is unavailable but the other one is installed, the
daemon prints a warning and falls back automatically. If neither backend is
installed, it prints an error and exits without monitoring devices. The flag is
ignored on Windows.

SwayNC treats a zero expiration timeout as "use the configured default" for
normal notifications. The daemon therefore uses a very large positive timeout
for persistent layer and drag-scroll "on" notifications and closes them by ID
when the mode ends. Do Not Disturb hides floating notifications, but the daemon
still tracks them.

### Windows 10/11

- Python 3.11 to 3.14 (at the time of writing, `hidapi` does not yet ship
  prebuilt wheels for Python 3.15; use an older interpreter so you do not need
  Visual C++ Build Tools).
- `hidapi` (installed by `requirements.txt`). The overlay uses `tkinter`, which
  ships with the official CPython Windows installer.
- Run the daemon **in your interactive user session**. The overlay needs a
  desktop; it cannot be shown from a Windows service.

Run from source:

```bat
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

Build the standalone executable on Windows (PyInstaller does not
cross-compile):

```bat
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python -m PyInstaller qmk-layer-notifier.spec
dist\qmk-layer-notifier.exe
```

The spec embeds `assets/keyboard.png` and `assets/mouse.png`. PyInstaller picks
up `tkinter` automatically.

To start the daemon automatically at logon, create a Task Scheduler task that
runs `dist\qmk-layer-notifier.exe` (or `pythonw.exe main.py`) in your user
session, for example with the "At log on" trigger.

#### Windows overlay

Windows toast notifications go through the WinRT notification platform, which
queues and animates them. That adds hundreds of milliseconds to several seconds
of latency, which is too slow for layer changes. The daemon therefore draws its
own overlay: a borderless, always-on-top, click-through window that does not
take focus.

The window is created once on a dedicated thread and kept hidden. Each layer or
scroll change updates the text in place, shows the overlay at the bottom center
of the monitor under the cursor, fades it in, and hides it again after about
1.6 seconds. Rapid changes are coalesced, so only the newest state is displayed
and no backlog builds up. The keyboard/mouse icon is loaded from `assets/`.

No registry changes or AUMID installation is required. Because the overlay is
a normal topmost window, it also appears over borderless fullscreen
applications; exclusive fullscreen games may still cover it.

If `tkinter` is unavailable or the window cannot be created, the daemon logs a
clear error once and keeps running: layer changes are still printed to stdout,
only the overlay is disabled.

## QMK firmware requirements

Enable QMK's USB console in the firmware's `rules.mk`:

```make
CONSOLE_ENABLE = yes
```

In your keymap's `keymap.c`, emit a newline-terminated console message whenever
the layer state changes. For example:

```c
#include "print.h"

#define DEVICE_TYPE "mouse"
#define DEVICE_NAME "Adept"

layer_state_t layer_state_set_user(layer_state_t state) {
    uint8_t layer = get_highest_layer(
        state | default_layer_state
    );

    uprintf("QMK_LAYER:%s:%s:%u\n",
            DEVICE_TYPE,
            DEVICE_NAME,
            layer);

    return state;
}
```

if your device supports drag-scroll (like the ploopy trackball lineup), also add this function to the firmware
```c 
bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    static bool scroll_enabled = false;

    if (keycode == DRAG_SCROLL && record->event.pressed) {
        scroll_enabled = !scroll_enabled;
        uprintf("QMK_SCROLL:%s:%s:%u\n",
                DEVICE_TYPE, DEVICE_NAME, scroll_enabled);
    }

    return true; // Let Ploopy handle DRAG_SCROLL normally.
}
```


If you already have a `layer_state_set_user` callback, add
the `uprintf` call to it rather than defining a second callback. Build and
flash the modified firmware using your usual QMK workflow. The example reports
the highest active layer, including the default layer when no other layer is
active; it emits on layer-state changes, not automatically on daemon startup.
With the default `swaync` backend, a layer message above layer 0 keeps a
notification visible until the next layer message. The layer 0 message closes
that notification and shows a short-lived "layer 0" message. Emit layer 0 when
returning to the base layer; the `layer_state_set_user` example does this because
it reports the highest active layer, including the default layer.

The accepted message format is:

```text
QMK_LAYER:<keyboard|mouse>:<device-name>:<decimal-layer>
```

For drag-scroll mode changes, the messages will also be emitted:

```text
QMK_SCROLL:mouse:Adept:1
QMK_SCROLL:mouse:Adept:0
```

Here `1` means enabled and `0` means disabled. Emit these when the mode changes
(not for each pointer movement). For Ploopy's default toggle behavior, the
keymap's `process_record_user` can emit them on `DRAG_SCROLL` presses; return
`true` so the keyboard's built-in handling still toggles the mode. A firmware
using momentary drag-scroll must report both presses and releases instead. With
the default `swaync` backend, the `1` message keeps a notification visible until
the matching `0` message arrives, so both transitions must be emitted.

Each message must end with a newline. For example,
`QMK_LAYER:keyboard:MyKeyboard:2` followed by `\n`. The
device name must not contain `:` or a newline. Keep messages short so they fit
comfortably in QMK's 32-byte console reports; the daemon also reassembles
lines spanning multiple reports. `keyboard` and `mouse` select the icon
(`input-keyboard`/`input-mouse` for SwayNC and SwayOSD, and the embedded keyboard
or mouse PNG in the Windows overlay).

`CONSOLE_ENABLE = yes` is required; `RAW_ENABLE = yes` alone will not work,
because this daemon does not listen on the Raw HID interface. A firmware that
does not emit messages in this format will not produce indicators.

## Troubleshooting

- No `Connected:` message:
  - Linux: check that the flashed firmware enables the USB console and that
    your user can read its `/dev/hidraw*` node. Configure appropriate udev
    permissions for your device rather than running the daemon as root in a
    graphical session.
  - Windows: check that the flashed firmware enables the USB console. No udev
    rules or administrator rights are normally required. If the device is still
    not detected, try another USB port or check Device Manager for a HID
    enumeration error.
- Connected, but no layer indicator: check the firmware's message format,
  newline, and layer-change callback.
- Layer changes appear in the terminal but no indicator is shown:
  - Linux: verify that the selected backend works from the same graphical
    session as the daemon. For `swaync`, run `notify-send "test" "test"` and
    check the session bus; for `swayosd`, run
    `swayosd-client --custom-message test`.
  - Linux: `Error: no notification backend found` means neither backend is
    installed or configured. Install SwayNC with `notify-send` and `gdbus`, or
    install SwayOSD.
  - Windows: check that the daemon runs in an interactive session and that
    `tkinter` works (`python -m tkinter`). The overlay is independent of Focus
    Assist and notification settings. Overlay errors are logged and do not stop
    the daemon.
- Windows: `OSD disabled: cannot import tkinter` means Tk support is missing.
  The official CPython Windows installer includes it; the daemon continues to
  print layer changes to stdout.
- Windows: PyInstaller bundles `tkinter` automatically. If the built executable
  cannot create the overlay, rebuild from `qmk-layer-notifier.spec`.
- Linux: layer or drag-scroll "on" notifications disappear on their own with
  `--output swayosd`; that backend is transient by design. Use the default
  `swaync` backend for persistent indicators.
- Linux: layer notifications stay visible after returning to the base layer with
  `--output swaync`: check that the firmware sends `QMK_LAYER:...:0` and that
  `gdbus call ... CloseNotification <id>` works from the daemon's session.
- Linux: drag-scroll "on" stays visible after leaving the mode with
  `--output swaync`: check that the firmware sends `QMK_SCROLL:...:0` and that
  `gdbus call ... CloseNotification <id>` works from the daemon's session.
