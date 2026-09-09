#!/usr/bin/env python3
"""Windowless launcher: serve the web interface from the system tray.

Runs the same uvicorn server as ``serve.py`` but with no console window.
Instead it registers a taskbar (notification-area) icon with two actions:

    * Open UI   - open the interface in the default browser
    * Copy URL  - copy the interface URL to the clipboard
    * Close     - stop the server and remove the icon

Run it directly for a normal (console) launch, or via ``pythonw.exe`` /
``SSH-Sync.vbs`` for a fully hidden one:

    pythonw tray.py [--host HOST] [--port PORT] [--config PATH]
"""

import os
import subprocess
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


def _copy_to_clipboard(text):
    """Put `text` on the Windows clipboard via the built-in clip.exe.

    clip.exe stores stdin verbatim and the value survives this process, so the
    copied URL stays available after the tray is closed. On any other platform
    (or if clip is missing) this is a no-op rather than an error.
    """
    # Imported lazily: importing anything under `sshsync` pulls in
    # `sshsync.paths`, which resolves the config path from the environment at
    # import time. main() must set SSH_SYNC_CONFIG first, so keep sshsync out of
    # this module's top-level imports.
    from sshsync.commands import no_window_flags

    try:
        subprocess.run(
            ["clip"],
            input=text,
            text=True,
            check=True,
            creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        pass

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

    # Build the app instance here (rather than passing uvicorn the factory
    # string) so the tray holds a reference to its RunManager and can cancel an
    # in-progress run before shutting down. Import is deferred until after the
    # config-path environment is set above, since sshsync resolves it on import.
    from sshsync.web.app import create_app

    app = create_app()
    manager = app.state.manager

    config = uvicorn.Config(
        app,
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

    def on_copy_url(icon, item):
        _copy_to_clipboard(url)

    def on_close(icon, item):
        # Stop any active run cleanly before tearing the process down. cancel()
        # terminates the backend subprocesses and lets the run thread mark jobs
        # CANCELLED and finalize stats/logs; without this the daemon run thread
        # would be killed mid-copy when the process exits, orphaning rclone.
        try:
            if manager.is_running:
                manager.cancel()
                manager.wait(timeout=30)
        except Exception:
            pass
        server.should_exit = True
        server_thread.join(timeout=10)
        icon.stop()

    icon = pystray.Icon(
        "ssh-sync",
        icon=_icon_image(),
        title=f"SSH-Sync ({url})",
        menu=pystray.Menu(
            pystray.MenuItem("Open UI", on_open, default=True),
            pystray.MenuItem("Copy URL", on_copy_url),
            pystray.MenuItem("Close", on_close),
        ),
    )
    icon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
