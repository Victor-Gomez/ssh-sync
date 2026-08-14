"""Failure logging.

The live table only shows the last error line, which is rarely enough to
diagnose anything, so failures also get their command and output tail written
to a dated log file.
"""

import threading
from datetime import datetime

from .paths import LOG_DIR

# How much backend output to keep per job for the failure log.
LOG_TAIL_LINES = 400

_WRITE_LOCK = threading.Lock()

_SEPARATOR = "=" * 78


def log_path_for_today():
    """Return the log file path for the current date."""
    return LOG_DIR / f"sync-{datetime.now().strftime('%Y-%m-%d')}.log"


def write_failure_log(job_name, server, command, exit_code, stats, tail_lines):
    """Append a failed job's command and output tail to today's log.

    Returns the log path, or None if it could not be written -- logging must
    never take down a sync run.
    """
    try:
        with _WRITE_LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = log_path_for_today()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_SEPARATOR + "\n")
                handle.write(
                    f"{datetime.now().isoformat(timespec='seconds')}  "
                    f"FAILED  {job_name} ({server})  exit={exit_code}\n"
                )
                handle.write(f"command: {' '.join(str(arg) for arg in command)}\n")
                handle.write(
                    f"errors={stats.get('error_count', 0)} "
                    f"last_error={stats.get('last_error', '')!r}\n"
                )
                handle.write(f"--- last {len(tail_lines)} output lines ---\n")
                for line in tail_lines:
                    handle.write(line + "\n")
                handle.write("\n")
        return path
    except OSError:
        return None
