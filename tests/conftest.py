"""Shared fixtures.

The suite runs without rclone, robocopy, an SSH server or a network: anything
touching the outside world is either isolated to a tmp_path or stubbed.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sample_config():
    """A small but complete config exercising both backends."""
    return {
        "servers": [
            {
                "name": "NAS",
                "host": "192.168.1.10",
                "user": "backup",
                "ssh_key_path": "~/.ssh/id_ed25519",
                "port": 22,
            },
            {
                "name": "Local",
                "host": "localhost",
                "user": "me",
                "ssh_key_path": "~/.ssh/id_ed25519",
                "port": 2222,
            },
        ],
        "sync_jobs": [
            {
                "name": "Docs",
                "type": "robocopy",
                "source": "C:/Docs",
                "destination": "D:/Backup/Docs",
            },
            {
                "name": "Docs",
                "type": "rclone",
                "server": "NAS",
                "source": "D:/Backup/Docs",
                "destination": "/srv/Docs",
            },
            {
                "name": "Docs",
                "type": "rclone",
                "server": "Local",
                "source": "C:/Docs",
                "destination": "E:/Mirror/Docs",
            },
            {
                "name": "Archive",
                "type": "robocopy",
                "source": "C:/Archive",
                "destination": "D:/Backup/Archive",
                "enabled": False,
            },
        ],
    }


@pytest.fixture
def config_file(tmp_path, sample_config):
    """Write the sample config to a temp file and return its path."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(sample_config), encoding="utf-8")
    return path
