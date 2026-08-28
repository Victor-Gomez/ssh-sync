"""Persistent run history, stored as an append-only JSONL log."""

import copy
import json
import os
import threading
from datetime import datetime, timezone

from .paths import LEGACY_STATS_PATH, STATS_PATH
from .utils import format_elapsed, parse_elapsed_seconds

# Appends run from job lanes in parallel, so serialize writes to the log.
_WRITE_LOCK = threading.Lock()

# summarize() has to read the whole history, and the dashboard asks for it on
# every visit. Keyed on the log's identity, size and mtime, so any append --
# from this process or another -- misses the cache and forces a fresh pass.
_SUMMARY_CACHE = {"key": None, "value": None}
_SUMMARY_LOCK = threading.Lock()


def _history_fingerprint():
    """Identify the current state of the log file, or None if unreadable."""
    try:
        info = STATS_PATH.stat()
    except OSError:
        return None
    return (str(STATS_PATH), info.st_size, info.st_mtime_ns)


# Bytes pulled per backwards step when reading the tail of the log. One step
# covers a few hundred runs, so a limited read almost never needs a second.
TAIL_CHUNK_BYTES = 65536

# Extra lines read beyond the requested limit, so a handful of malformed ones
# in the tail cannot shrink the result below what the caller asked for.
TAIL_MARGIN_LINES = 32

_AGGREGATE_FIELDS = (
    "uploaded_files",
    "uploaded_bytes",
    "deleted_files",
    "deleted_bytes",
    "files_total",
    "files_total_bytes",
    "error_count",
)


def _encode_entry(entry):
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def _migrate_legacy_store():
    """Convert an old single-object sync-stats.json into the append-only log.

    The original file is kept alongside as sync-stats.json.migrated rather than
    deleted, so no history is lost if the conversion needs to be revisited.
    """
    if STATS_PATH.exists() or not LEGACY_STATS_PATH.is_file():
        return

    try:
        data = json.loads(LEGACY_STATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None

    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        jobs = []

    temp_path = STATS_PATH.with_name(STATS_PATH.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for item in jobs:
            if isinstance(item, dict):
                handle.write(_encode_entry(item) + "\n")
    temp_path.replace(STATS_PATH)

    LEGACY_STATS_PATH.replace(
        LEGACY_STATS_PATH.with_name(LEGACY_STATS_PATH.name + ".migrated")
    )


def _read_tail_lines(path, line_count):
    """Return roughly the last `line_count` lines of a file, newest last.

    Read backwards in chunks so the cost stays flat as the history grows: the
    dashboard asks for a couple of hundred runs out of a log that is now
    thousands long, and parsing all of it on every request is pure waste.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0 and buffer.count(b"\n") <= line_count:
            step = min(TAIL_CHUNK_BYTES, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer

    lines = buffer.split(b"\n")
    # Anything but a read that reached the start begins mid-line.
    if position > 0:
        lines = lines[1:]
    return [line.decode("utf-8", errors="replace") for line in lines]


def _parse_entries(lines):
    """Turn raw log lines into entry dicts, skipping ones that do not parse.

    Malformed lines are dropped rather than raised: a truncated final write
    must not make the whole history unreadable.
    """
    entries = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def load_entries(limit=None):
    """Read stored runs from the log, newest last.

    With a `limit`, only the tail of the file is touched.
    """
    _migrate_legacy_store()

    if not STATS_PATH.is_file():
        return []

    tailing = limit is not None and limit >= 0
    if tailing and limit == 0:
        return []

    try:
        if tailing:
            lines = _read_tail_lines(STATS_PATH, limit + TAIL_MARGIN_LINES)
        else:
            with STATS_PATH.open(encoding="utf-8") as handle:
                lines = handle.readlines()
    except OSError:
        return []

    entries = _parse_entries(lines)
    return entries[-limit:] if tailing else entries


def append_job_stats(job, stats, status, exit_code, source_bytes=0, dry_run=False):
    """Append one completed job result to the history.

    One line per run means the cost of recording a result stays constant no
    matter how long the history gets.
    """
    entry = {
        "ended_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "name": str(job.get("name", "default")),
        "type": str(job.get("type", "")),
        "server": (
            str(job.get("server", "local")) if job.get("type") == "rclone" else "local"
        ),
        "source": str(job.get("source", "")),
        "destination": str(job.get("destination", "")),
        "source_bytes": int(source_bytes),
        "status": str(status),
        "exit_code": int(exit_code),
        "dry_run": bool(dry_run),
        "stats": {
            "uploaded_files": int(stats.get("uploaded_files", 0)),
            "uploaded_bytes": int(stats.get("uploaded_bytes", 0)),
            "deleted_files": int(stats.get("deleted_files", 0)),
            "deleted_bytes": int(stats.get("deleted_bytes", 0)),
            "checks_done": int(stats.get("checks_done", 0)),
            "checks_total": int(stats.get("checks_total", 0)),
            "elapsed": str(stats.get("elapsed", "0s")),
            "error_count": int(stats.get("error_count", 0)),
            "last_error": str(stats.get("last_error", "")),
        },
    }

    with _WRITE_LOCK:
        _migrate_legacy_store()
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with STATS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(_encode_entry(entry) + "\n")

    return entry


def _new_aggregate(**extra):
    aggregate = dict.fromkeys(_AGGREGATE_FIELDS, 0)
    aggregate.update(runs=0, failures=0, elapsed_seconds=0, last_ended_at="")
    aggregate.update(extra)
    return aggregate


def _accumulate(target, entry):
    """Fold one history entry into an aggregate bucket."""
    stats = entry.get("stats", {})
    target["runs"] += 1
    target["uploaded_files"] += int(stats.get("uploaded_files", 0))
    target["uploaded_bytes"] += int(stats.get("uploaded_bytes", 0))
    target["deleted_files"] += int(stats.get("deleted_files", 0))
    target["deleted_bytes"] += int(stats.get("deleted_bytes", 0))
    target["files_total"] += int(stats.get("checks_total", 0))
    target["files_total_bytes"] += int(entry.get("source_bytes", 0))
    target["error_count"] += int(stats.get("error_count", 0))
    target["elapsed_seconds"] += parse_elapsed_seconds(stats.get("elapsed", "0s"))

    ended_at = str(entry.get("ended_at", ""))
    if ended_at > target["last_ended_at"]:
        target["last_ended_at"] = ended_at

    if str(entry.get("status", "")).upper() != "OK":
        target["failures"] += 1

    return target


def summarize(entries=None):
    """Roll the history up into overall, per-job and per-server totals.

    A single pass feeds all three views, so the cost stays linear in history
    size even as the log grows to thousands of runs.
    """
    if entries is not None:
        return _summarize_entries(entries)

    fingerprint = _history_fingerprint()
    with _SUMMARY_LOCK:
        if fingerprint is not None and _SUMMARY_CACHE["key"] == fingerprint:
            return copy.deepcopy(_SUMMARY_CACHE["value"])

    summary = _summarize_entries(load_entries())

    if fingerprint is not None and fingerprint == _history_fingerprint():
        with _SUMMARY_LOCK:
            _SUMMARY_CACHE["key"] = fingerprint
            _SUMMARY_CACHE["value"] = copy.deepcopy(summary)
    return summary


def _summarize_entries(entries):
    """Fold history entries into the overall, per-job and per-server views."""
    totals = _new_aggregate()
    jobs = {}
    servers = {}

    for entry in entries:
        _accumulate(totals, entry)

        job_key = (
            str(entry.get("name", "")),
            str(entry.get("type", "")),
            str(entry.get("server", "")),
        )
        if job_key not in jobs:
            jobs[job_key] = _new_aggregate(
                name=job_key[0], type=job_key[1], server=job_key[2]
            )
        _accumulate(jobs[job_key], entry)

        server_key = str(entry.get("server", "")) or "unknown"
        if server_key not in servers:
            servers[server_key] = _new_aggregate(server=server_key)
        _accumulate(servers[server_key], entry)

    return {
        "totals": totals,
        "jobs": [jobs[key] for key in sorted(jobs, key=lambda k: (k[1], k[2], k[0]))],
        "servers": [servers[key] for key in sorted(servers)],
        "stats_path": str(STATS_PATH),
    }


def format_ended_at(ended_at_value):
    """Format a stored ISO timestamp as `yyyy-MM-dd HH:mm:ss` for display."""
    raw_text = str(ended_at_value or "").strip()
    if not raw_text:
        return ""

    normalized = raw_text[:-1] + "+00:00" if raw_text.endswith("Z") else raw_text
    try:
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw_text


def format_aggregate_elapsed(aggregate):
    """Format an aggregate's accumulated runtime."""
    return format_elapsed(aggregate["elapsed_seconds"])
