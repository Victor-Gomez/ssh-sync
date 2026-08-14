"""Scheduling: which jobs may run in parallel, and which must wait.

Kept free of I/O so the whole schedule can be computed - and tested - without
touching a disk or a network.
"""

from .utils import expand_path

# Hosts whose "remote" destination is really this machine's own filesystem.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def is_loopback_host(host):
    """Report whether an rclone server actually writes to this machine."""
    return str(host or "").strip().lower() in LOOPBACK_HOSTS


def normalize_local_path(value):
    """Normalize a path for overlap comparison between jobs."""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""

    # Remote-style paths carry a leading slash before the drive letter.
    if len(text) >= 3 and text[0] == "/" and text[1].isalpha() and text[2] == ":":
        text = text[1:]

    try:
        text = expand_path(text)
    except (OSError, ValueError):
        pass

    return text.rstrip("/").lower()


def paths_overlap(first, second):
    """Report whether two normalized paths refer to the same tree."""
    if not first or not second:
        return False
    if first == second:
        return True
    return first.startswith(second + "/") or second.startswith(first + "/")


def build_job_lanes(jobs):
    """Group jobs into lanes that may run concurrently with one another.

    Jobs within a lane run sequentially. Each rclone server gets its own lane so
    two servers sync in parallel, while all robocopy jobs share a single lane to
    avoid several local mirrors thrashing the same disks at once.
    """
    lanes = {}
    for index, job in enumerate(jobs):
        if str(job.get("type", "")).lower() == "rclone":
            lane_key = ("rclone", str(job.get("server", "")))
        else:
            lane_key = ("robocopy", "")
        lanes.setdefault(lane_key, []).append(index)
    return lanes


def _job_io_paths(job, servers_by_name):
    """Return the `(read, write)` local paths a job touches.

    A job's write path is empty when it lands on a genuinely remote machine,
    since nothing local can then contend with it.
    """
    read = normalize_local_path(job.get("source", ""))

    if str(job.get("type", "")).lower() == "robocopy":
        return read, normalize_local_path(job.get("destination", ""))

    server = servers_by_name.get(str(job.get("server", ""))) or {}
    if is_loopback_host(server.get("host", "")):
        # An rclone job pointed at loopback lands on this filesystem, so it
        # contends with local jobs exactly like a robocopy destination.
        return read, normalize_local_path(job.get("destination", ""))

    return read, ""


def build_job_dependencies(jobs, lanes, servers_by_name):
    """Find ordering constraints that survive splitting jobs across lanes.

    Some jobs feed others: a robocopy job writes a tree that a later rclone job
    uploads. Running those concurrently would capture a half-written source, so
    any pair touching overlapping local paths keeps its original config order.
    Jobs already sharing a lane are ordered by the lane itself.

    Returns a mapping of job index -> set of indexes that must finish first.
    """
    lane_of = {
        index: lane_key for lane_key, indexes in lanes.items() for index in indexes
    }

    io_paths = [_job_io_paths(job, servers_by_name) for job in jobs]

    dependencies = {index: set() for index in range(len(jobs))}
    for later in range(len(jobs)):
        later_read, later_write = io_paths[later]
        for earlier in range(later):
            if lane_of[earlier] == lane_of[later]:
                continue
            earlier_read, earlier_write = io_paths[earlier]
            if (
                paths_overlap(earlier_write, later_read)
                or paths_overlap(earlier_write, later_write)
                or paths_overlap(earlier_read, later_write)
            ):
                dependencies[later].add(earlier)

    return dependencies


def required_rclone_servers(jobs):
    """List the distinct servers the given rclone jobs need, in config order."""
    servers = []
    for job in jobs:
        if str(job.get("type", "")).lower() != "rclone":
            continue
        name = job.get("server")
        if name and name not in servers:
            servers.append(name)
    return servers
