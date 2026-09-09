#!/usr/bin/env python3
"""Windowless launcher: serve the web interface from the system tray.

Runs the same uvicorn server as ``serve.py`` but with no console window.
Instead it registers a taskbar (notification-area) icon with two actions:

    * Open UI  - open the interface in the default browser
    * Close    - stop the server and remove the icon

Run it directly for a normal (console) launch, or via ``pythonw.exe`` /
``SSH-Sync.vbs`` for a fully hidden one:

    pythonw tray.py [--host HOST] [--port PORT] [--config PATH]
"""

import os
import sys
import threading
import webbrowser

# Under pythonw.exe (the windowless launch) there is no console, so
# sys.stdout/sys.stderr are None. uvicorn's log formatter calls
# sys.stdout.isatty() while configuring logging and would crash on startup,
# so give both streams a real sink before anything touches them.
if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

import uvicorn

from serve import parse_args

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit(
        "The tray launcher needs 'pystray' and 'pillow'.\n"
        "Install them with:  pip install pystray pillow"
    ) from exc


# The two arrow polygons from sshsync/web/static/logo.svg (90x90 viewBox),
# redrawn here so the tray icon matches the app logo without an SVG renderer.
_BRAND = (59, 130, 246)  # #3b82f6
_LOGO_PATHS = (
    ((0, 0), (30, 15), (15, 23.333), (15, 66.667), (60, 45), (60, 60), (0, 90)),
    ((90, 0), (90, 90), (60, 75), (75, 66.667), (75, 23.333), (30, 45), (30, 30)),
)


def _icon_image(size=64):
    """Render the app logo as a transparent PNG for the tray icon."""
    scale = size / 90
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for path in _LOGO_PATHS:
        draw.polygon([(x * scale, y * scale) for x, y in path], fill=_BRAND)
    return img


def main(argv=None):
    """Start the server in a background thread and run the tray icon."""
    args = parse_args(argv)

    if args.config:
        from pathlib import Path

        config_path = Path(args.config).expanduser().resolve()
        if not config_path.is_file():
            raise SystemExit(f"[ERROR] No such config file: {config_path}")
        os.environ["SSH_SYNC_CONFIG"] = str(config_path)

    url = f"http://{args.host}:{args.port}"

    config = uvicorn.Config(
        "sshsync.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    # Install signal handlers only on the main thread; here we run in a worker.
    server.install_signal_handlers = lambda: None

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    def on_open(icon, item):
        webbrowser.open(url)

    def on_close(icon, item):
        server.should_exit = True
        server_thread.join(timeout=10)
        icon.stop()

    icon = pystray.Icon(
        "ssh-sync",
        icon=_icon_image(),
        title=f"SSH-Sync ({url})",
        menu=pystray.Menu(
            pystray.MenuItem("Open UI", on_open, default=True),
            pystray.MenuItem("Close", on_close),
        ),
    )
    icon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
