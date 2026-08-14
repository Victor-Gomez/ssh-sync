#!/usr/bin/env python3
"""CLI entry point: run sync jobs with a live progress table.

Usage:
    python sync.py [--job SELECTOR] [--dry-run] [--stats]
"""

import sys

from sshsync.cli import run

if __name__ == "__main__":
    sys.exit(run())
