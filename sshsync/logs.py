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
