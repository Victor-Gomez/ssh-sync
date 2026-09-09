"""Subprocess execution and real-time parsing of backend output."""

import re
import subprocess
import time

from .commands import no_window_flags
from .utils import format_elapsed

# Every job reports the same shape so the CLI table, the web UI and the stats
# store can all read one record without knowing which backend produced it.
STATS_FIELDS = (
    "uploaded_files",
    "uploaded_bytes",
    "deleted_files",
    "deleted_bytes",
    "checks_done",
    "checks_total",
    "error_count",
)


def new_stats():
    """Build an empty stats record."""
    stats = dict.fromkeys(STATS_FIELDS, 0)
    stats["elapsed"] = "0s"
    stats["last_error"] = ""
    return stats


_BINARY_FACTORS = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "PiB": 1024**5,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "PB": 1000**5,
}

_SHORT_FACTORS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}

# A size token as rclone prints it, e.g. "1.234 GiB", "512 B" or the SI "1 kB"
# (rclone lowercases only the kilo prefix, matching the SI convention).
_SIZE_TOKEN = r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKMGTPE]?i?B"
_SIZE_PATTERN = re.compile(r"^([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKMGTPE]?i?B)$")

# Robocopy prints sizes with an optional unit letter and inconsistent casing,
# e.g. "1234", "12.3 m", "4.5 GB".
_ROBOCOPY_SIZE_PATTERN = re.compile(
    r"^([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMGTP]?)(?:i?[bB])?$", re.IGNORECASE
)
_ROBOCOPY_SIZE_TOKEN = r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMGTP]?(?:i?[bB])?"


def parse_size(size_text):
    """Parse an rclone size token into bytes, returning 0 when unrecognised."""
    match = _SIZE_PATTERN.match(str(size_text).strip())
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    return int(number * _BINARY_FACTORS.get(match.group(2), 1))


def parse_robocopy_size(size_text):
    """Parse a robocopy size token into bytes, returning 0 when unrecognised."""
    match = _ROBOCOPY_SIZE_PATTERN.match(str(size_text).strip())
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    return int(number * _SHORT_FACTORS.get(match.group(2).upper(), 1))


class RcloneParser:
    """Extract progress from rclone's `--stats` summary blocks."""

    mode = "rclone"

    _TRANSFERRED_BYTES = re.compile(rf"Transferred:\s+({_SIZE_TOKEN})\s*/")
    _TRANSFERRED_FILES = re.compile(r"Transferred:\s+(\d+)\s*/\s*\d+")
    _DELETED_FILES = re.compile(r"Deleted:\s+(\d+)\s+\(files\)")
    _DELETED_BYTES = re.compile(rf",\s*({_SIZE_TOKEN})\s+\(freed\)")
    _CHECKS = re.compile(r"Checks:\s+(\d+)\s*/\s*(\d+)")
    _ELAPSED = re.compile(r"Elapsed time:\s+(.+)")

    def feed(self, stats, line):
        """Fold one output line into the running stats record."""
        if " ERROR : " in line:
            stats["error_count"] += 1
            stats["last_error"] = line.split(" ERROR : ", 1)[1].strip()

        if line.startswith("Transferred:"):
            # rclone prints two "Transferred:" lines: bytes first, then files.
            match = self._TRANSFERRED_BYTES.search(line)
            if match:
                stats["uploaded_bytes"] = parse_size(match.group(1))
            match = self._TRANSFERRED_FILES.search(line)
            if match:
                stats["uploaded_files"] = int(match.group(1))

        elif line.startswith("Deleted:"):
            match = self._DELETED_FILES.search(line)
            if match:
                stats["deleted_files"] = int(match.group(1))
            match = self._DELETED_BYTES.search(line)
            if match:
                stats["deleted_bytes"] = parse_size(match.group(1))

        elif line.startswith("Checks:"):
            match = self._CHECKS.search(line)
            if match:
                stats["checks_done"] = int(match.group(1))
                stats["checks_total"] = int(match.group(2))

        elif line.startswith("Elapsed time:"):
            match = self._ELAPSED.search(line)
            if match:
                stats["elapsed"] = match.group(1).strip()

    def finalize(self, stats, exit_code):
        """Apply end-of-run corrections. rclone needs none."""


class RobocopyParser:
    """Extract progress from robocopy's end-of-run summary table."""

    mode = "robocopy"

    # Files : Total Copied Skipped Mismatch FAILED Extras
    _FILES = re.compile(
        r"^\s*Files\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", re.IGNORECASE
    )
    _BYTES = re.compile(
        r"^\s*Bytes\s*:\s*" + r"\s+".join([f"({_ROBOCOPY_SIZE_TOKEN})"] * 6),
        re.IGNORECASE,
    )

    def __init__(self):
        self._last_error_line = ""

    def feed(self, stats, line):
        """Fold one output line into the running stats record."""
        if "ERROR" in line.upper():
            self._last_error_line = line.strip()

        match = self._FILES.match(line)
        if match:
            total, copied, skipped, mismatch, failed, extras = (
                int(value) for value in match.groups()
            )
            stats["uploaded_files"] = copied
            stats["deleted_files"] = extras
            stats["checks_total"] = total
            stats["checks_done"] = min(total, copied + skipped + mismatch + failed)
            if failed:
                stats["error_count"] = max(stats["error_count"], failed)
            return

        match = self._BYTES.match(line)
        if match:
            stats["uploaded_bytes"] = parse_robocopy_size(match.group(2))
            stats["deleted_bytes"] = parse_robocopy_size(match.group(6))

    def finalize(self, stats, exit_code):
        """Reconcile errors against the exit code.

        Robocopy emits transient ERROR lines for files it then retries
        successfully, so those only count as real errors when the exit code
        (a bitmask, >= 8 meaning failure) agrees.
        """
        if exit_code < 8:
            stats["error_count"] = 0
            stats["last_error"] = ""
            return

        stats["error_count"] = max(stats["error_count"], 1)
        stats["last_error"] = (
            self._last_error_line or f"Robocopy failed with exit code {exit_code}"
        )


PARSERS = {"rclone": RcloneParser, "robocopy": RobocopyParser}


def _is_error_line(line, parser_mode):
    """Report whether an output line carries a backend error worth keeping.

    rclone tags errors as ` ERROR : `; robocopy prints an all-caps `ERROR`
    followed by the failing path. Both are rare next to the per-second progress
    spam, so they are what a failure log actually needs.
    """
    if parser_mode == "robocopy":
        return "ERROR" in line.upper()
    return " ERROR : " in line


def stream_command(
    command,
    on_update=None,
    parser_mode="rclone",
    on_process=None,
    line_buffer=None,
    error_buffer=None,
):
    """Run a backend command, parsing its output into stats as it streams.

    Args:
        command: Argument list to execute.
        on_update: Called with a stats snapshot after each parsed line.
        parser_mode: Which output dialect to expect ("rclone" or "robocopy").
        on_process: Called as `(process, running)` when the child starts and
            exits, so the caller can terminate it on cancellation.
        line_buffer: Optional deque receiving every output line. Give it a
            maxlen so failures can be logged with context without holding the
            whole transcript in memory.
        error_buffer: Optional deque receiving only the error lines. Kept apart
            from `line_buffer` because a long run's per-second stats otherwise
            push the handful of error lines -- the ones that explain the
            failure -- out of the tail before it is logged.

    Returns:
        Tuple of `(exit_code, stats)`.
    """
    parser_class = PARSERS.get(parser_mode)
    if parser_class is None:
        raise ValueError(f"Unknown parser mode '{parser_mode}'.")
    parser = parser_class()

    stats = new_stats()
    started_at = time.monotonic()

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=no_window_flags(),
    )

    if on_process is not None:
        on_process(process, True)

    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if line_buffer is not None:
                line_buffer.append(line)
            if error_buffer is not None and _is_error_line(line, parser_mode):
                error_buffer.append(line)

            stats["elapsed"] = format_elapsed(time.monotonic() - started_at)
            parser.feed(stats, line)
            if on_update is not None:
                on_update(dict(stats))

        process.wait()
    finally:
        if on_process is not None:
            on_process(process, False)

    stats["elapsed"] = format_elapsed(time.monotonic() - started_at)
    parser.finalize(stats, process.returncode)

    if on_update is not None:
        on_update(dict(stats))
    return process.returncode, stats
