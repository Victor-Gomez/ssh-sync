"""Path handling and display formatting helpers."""

import os
from pathlib import Path

_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def expand_path(path_value):
    """Expand environment variables and `~`, then resolve to a posix path."""
    expanded = os.path.expandvars(os.path.expanduser(str(path_value)))
    return Path(expanded).resolve().as_posix()


def normalize_remote_path(remote_path):
    """Normalize a destination path for rclone's SFTP backend.

    Windows-style drive paths need a leading slash (`D:/Fotos` -> `/D:/Fotos`)
    or rclone reads the colon as a remote separator.
    """
    normalized = str(remote_path).replace("\\", "/").rstrip("/")
    if (
        len(normalized) >= 2
        and normalized[1] == ":"
        and normalized[0].isalpha()
        and not normalized.startswith("/")
    ):
        normalized = "/" + normalized
    return normalized


def require_job_value(job, key, job_type):
    """Return a required job field, raising a job-scoped error when missing."""
    value = job.get(key)
    if value is None or str(value).strip() == "":
        name = str(job.get("name", "default"))
        raise RuntimeError(f"Missing required key '{key}' in {job_type} job '{name}'.")
    return value


def format_bytes(size_bytes):
    """Render a byte count as human-readable binary units."""
    value = float(size_bytes)
    for unit in _BINARY_UNITS:
        if value < 1024 or unit == _BINARY_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return "0 B"


def format_elapsed(elapsed_seconds):
    """Render a duration in seconds as compact `1h02m03s` style text."""
    total = max(0, int(elapsed_seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def parse_elapsed_seconds(elapsed_value):
    """Parse `12s`, `3m05s` or `1h02m03s` back into seconds.

    Returns 0 for anything unparseable, since these strings come from backend
    output and a malformed one must not break a stats roll-up.
    """
    text = str(elapsed_value or "").strip().lower()
    if not text:
        return 0

    multipliers = {"h": 3600, "m": 60, "s": 1}
    total = 0
    number = ""
    for char in text:
        if char.isdigit():
            number += char
        elif char in multipliers and number:
            total += int(number) * multipliers[char]
            number = ""
        elif not char.isspace():
            return 0

    return total


def profile_directory(path_value, file_cap=None):
    """Walk a directory tree once, returning `(file_count, total_bytes)`.

    Symlinks are skipped and unreadable entries ignored, exactly as
    `get_directory_size_bytes` does: the figures are advisory (a display aid and
    a concurrency hint), so a locked file must not abort the walk.

    `file_cap`, when set, stops the walk once that many files have been counted.
    Past the cap the tree is already firmly "many small files", so the tuning it
    feeds is saturated and walking the rest only burns time.
    """
    file_count = 0
    total_size = 0
    # Keep the stack as plain strings: os.scandir accepts them directly and this
    # avoids building a Path object for every directory in the tree.
    stack = [os.fspath(path_value)]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            file_count += 1
                            total_size += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue

        if file_cap is not None and file_count >= file_cap:
            return file_count, total_size

    return file_count, total_size


def get_directory_size_bytes(path_value):
    """Recursively total the size of a directory tree, skipping symlinks.

    Unreadable entries are skipped rather than raised: this figure is only a
    display aid, so a locked file must not abort the walk.
    """
    _, total_size = profile_directory(path_value)
    return total_size


def is_failed_exit_code(job_type, exit_code):
    """Report whether a backend exit code means the job failed.

    Robocopy uses a bitmask where anything below 8 reports work done (files
    copied, extras removed) rather than an error.
    """
    if job_type == "robocopy":
        return exit_code >= 8
    return exit_code != 0
