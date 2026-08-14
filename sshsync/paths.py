"""Filesystem locations shared by the CLI, the web app and the stats store.

Every path is resolved from the project root rather than the current working
directory, so the tool behaves the same whether it is launched from a shortcut,
a scheduled task, or a shell sitting somewhere else.
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

# Overridable so tests (and side-by-side setups) can point at another config.
CONFIG_PATH = Path(os.environ.get("SSH_SYNC_CONFIG") or PROJECT_ROOT / "config.json")
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.json"

STATS_PATH = PROJECT_ROOT / "sync-stats.jsonl"
LEGACY_STATS_PATH = PROJECT_ROOT / "sync-stats.json"


def _default_log_dir():
    """Pick a per-user log directory outside any synced tree.

    Logs must not live inside a source directory: a file that changes on every
    run gets re-uploaded every run, and can be held open while a backend tries
    to replace it.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "SSH-Sync" / "logs"
    return Path.home() / ".local" / "share" / "ssh-sync" / "logs"


LOG_DIR = _default_log_dir()
