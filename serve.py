#!/usr/bin/env python3
"""Web entry point: serve the management interface.

Usage:
    python serve.py [--host HOST] [--port PORT] [--config PATH] [--reload]

Binds to localhost by default. The interface has no authentication and can
trigger local backends, so only expose it on a wider interface if the network
is one you control.
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420


def parse_args(argv=None):
    """Parse server options."""
    parser = argparse.ArgumentParser(prog="serve", description=__doc__.split("\n")[0])
    parser.add_argument("--host", default=DEFAULT_HOST, help="Interface to bind.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind.")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Serve a config file other than the default.",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Restart on code changes (development)."
    )
    return parser.parse_args(argv)


def main(argv=None):
    """Start the uvicorn server."""
    args = parse_args(argv)

    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.is_file():
            print(f"[ERROR] No such config file: {config_path}", file=sys.stderr)
            return 1
        # The app resolves its config path on import, and with --reload that
        # import happens in a worker process, so pass the choice through the
        # environment rather than a module attribute.
        os.environ["SSH_SYNC_CONFIG"] = str(config_path)
        print(f"Config: {config_path}")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[WARN] Binding to {args.host}: the interface is unauthenticated.",
            file=sys.stderr,
        )

    print(f"SSH-Sync web interface: http://{args.host}:{args.port}")
    uvicorn.run(
        "sshsync.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
