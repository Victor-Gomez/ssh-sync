"""Failure logging.

The live table only shows the last error line, which is rarely enough to
diagnose anything, so failures also get their command and output tail written
to a dated log file.
"""

import re
import threading
from datetime import datetime

from .paths import LOG_DIR

# How much backend output to keep per job for the failure log.
LOG_TAIL_LINES = 400

# How many error lines to keep per job. Held separately from the tail because a
# long run's per-second progress blocks would otherwise evict the few error
# lines that actually explain the failure (e.g. which files could not be
# deleted) before the log is written.
LOG_ERROR_LINES = 200

LOG_NAME_PREFIX = "sync-"
LOG_NAME_SUFFIX = ".log"

# Dates arrive from the browser as a query parameter, so they are matched
# strictly rather than pasted into a path: nothing with a separator in it can
# get through and address a file outside the log directory.
DATE_FORMAT = "%Y-%m-%d"
_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

_WRITE_LOCK = threading.Lock()

_SEPARATOR = "=" * 78


def log_path_for_date(date_text):
    """Return the log file path for an ISO date, rejecting anything else."""
    text = str(date_text or "").strip()
    if not _DATE_PATTERN.match(text):
        raise ValueError(f"Invalid log date '{date_text}'. Use YYYY-MM-DD.")
    try:
        datetime.strptime(text, DATE_FORMAT)
    except ValueError as exc:
        raise ValueError(f"Invalid log date '{date_text}'. Use YYYY-MM-DD.") from exc
    return LOG_DIR / f"{LOG_NAME_PREFIX}{text}{LOG_NAME_SUFFIX}"


def today_text():
    """Return today's date in the form used by log file names."""
    return datetime.now().strftime(DATE_FORMAT)


def log_path_for_today():
    """Return the log file path for the current date."""
    return log_path_for_date(today_text())


def available_log_dates():
    """List the dates that have a failure log, newest first.

    A missing or unreadable log directory is simply an empty history: nothing
    has failed yet, which is not itself an error worth surfacing.
    """
    try:
        names = [
            path.name
            for path in LOG_DIR.glob(f"{LOG_NAME_PREFIX}*{LOG_NAME_SUFFIX}")
            if path.is_file()
        ]
    except OSError:
        return []

    dates = {name[len(LOG_NAME_PREFIX) : -len(LOG_NAME_SUFFIX)] for name in names}
    return sorted((date for date in dates if _DATE_PATTERN.match(date)), reverse=True)


def write_failure_log(
    job_name, server, command, exit_code, stats, tail_lines, error_lines=None
):
    """Append a failed job's command, error lines and output tail to today's log.

    `error_lines` are the backend's error lines, kept apart from `tail_lines` so
    they survive a long run's progress spam; they are written first because they
    are what a reader needs. Returns the log path, or None if it could not be
    written -- logging must never take down a sync run.
    """
    error_lines = error_lines or []
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
                if error_lines:
                    handle.write(f"--- {len(error_lines)} error line(s) ---\n")
                    for line in error_lines:
                        handle.write(line + "\n")
                handle.write(f"--- last {len(tail_lines)} output lines ---\n")
                for line in tail_lines:
                    handle.write(line + "\n")
                handle.write("\n")
        return path
    except OSError:
        return None
