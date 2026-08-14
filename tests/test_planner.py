"""Tests for lane grouping and cross-lane dependency detection."""

from sshsync.planner import (
    build_job_dependencies,
    build_job_lanes,
    is_loopback_host,
    paths_overlap,
    required_rclone_servers,
)

SERVERS = {
    "NAS": {"name": "NAS", "host": "192.168.1.10"},
    "Local": {"name": "Local", "host": "localhost"},
}


def plan(jobs, servers=None):
    """Build lanes and dependencies for a job list."""
    lanes = build_job_lanes(jobs)
    return lanes, build_job_dependencies(jobs, lanes, servers or SERVERS)


def robocopy(name, source, destination):
    return {
        "name": name,
        "type": "robocopy",
        "source": source,
        "destination": destination,
    }


def rclone(name, server, source, destination):
    return {
        "name": name,
        "type": "rclone",
        "server": server,
        "source": source,
        "destination": destination,
    }


# -- primitives ------------------------------------------------------------


def test_loopback_hosts_are_recognised():
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("  ::1 ")
    assert is_loopback_host("")
    assert not is_loopback_host("192.168.1.10")


def test_paths_overlap_covers_nesting_and_siblings():
    assert paths_overlap("c:/a", "c:/a")
    assert paths_overlap("c:/a", "c:/a/b")
    assert paths_overlap("c:/a/b", "c:/a")
    assert not paths_overlap("c:/a", "c:/ab")
    assert not paths_overlap("c:/a", "")


# -- lanes -----------------------------------------------------------------


def test_each_rclone_server_gets_its_own_lane():
    jobs = [
        robocopy("A", "C:/a", "D:/a"),
        robocopy("B", "C:/b", "D:/b"),
        rclone("C", "NAS", "C:/c", "/srv/c"),
        rclone("D", "Local", "C:/d", "E:/d"),
    ]
    lanes = build_job_lanes(jobs)

    assert lanes[("robocopy", "")] == [0, 1]
    assert lanes[("rclone", "NAS")] == [2]
    assert lanes[("rclone", "Local")] == [3]


def test_required_servers_are_listed_once_in_order():
    jobs = [
        rclone("A", "NAS", "C:/a", "/srv/a"),
        rclone("B", "Local", "C:/b", "E:/b"),
        rclone("C", "NAS", "C:/c", "/srv/c"),
        robocopy("D", "C:/d", "D:/d"),
    ]
    assert required_rclone_servers(jobs) == ["NAS", "Local"]


# -- dependencies ----------------------------------------------------------


def test_upload_waits_for_the_mirror_that_writes_its_source():
    # robocopy writes D:/Backup, then rclone uploads it: these must not overlap.
    jobs = [
        robocopy("Mirror", "C:/Docs", "D:/Backup/Docs"),
        rclone("Upload", "NAS", "D:/Backup/Docs", "/srv/Docs"),
    ]
    _, dependencies = plan(jobs)

    assert dependencies[1] == {0}
    assert dependencies[0] == set()


def test_independent_trees_run_in_parallel():
    jobs = [
        robocopy("Mirror", "C:/Docs", "D:/Backup/Docs"),
        rclone("Upload", "NAS", "C:/Photos", "/srv/Photos"),
    ]
    _, dependencies = plan(jobs)

    assert dependencies == {0: set(), 1: set()}


def test_remote_destinations_do_not_contend_locally():
    # Both upload to the same remote path, but neither writes locally.
    jobs = [
        rclone("A", "NAS", "C:/a", "/srv/shared"),
        rclone("B", "Local", "C:/b", "/srv/shared"),
    ]
    _, dependencies = plan(jobs)

    assert dependencies[1] == set()


def test_loopback_destination_contends_like_a_local_write():
    # The "remote" is this machine, so its destination is a real local write.
    jobs = [
        rclone("Down", "Local", "C:/src", "D:/Shared"),
        robocopy("Read", "D:/Shared", "E:/Copy"),
    ]
    _, dependencies = plan(jobs)

    assert dependencies[1] == {0}


def test_same_lane_jobs_need_no_explicit_dependency():
    # Both are robocopy, so the lane already serializes them.
    jobs = [
        robocopy("First", "C:/Docs", "D:/Stage"),
        robocopy("Second", "D:/Stage", "E:/Final"),
    ]
    lanes, dependencies = plan(jobs)

    assert lanes[("robocopy", "")] == [0, 1]
    assert dependencies[1] == set()


def test_nested_paths_are_treated_as_overlapping():
    jobs = [
        robocopy("Parent", "C:/src", "D:/Backup"),
        rclone("Child", "NAS", "D:/Backup/Photos/2024", "/srv/x"),
    ]
    _, dependencies = plan(jobs)

    assert dependencies[1] == {0}


def test_dependencies_only_point_backwards():
    jobs = [
        rclone("Upload", "NAS", "D:/Backup/Docs", "/srv/Docs"),
        robocopy("Mirror", "C:/Docs", "D:/Backup/Docs"),
    ]
    _, dependencies = plan(jobs)

    # Config order is authoritative: the upload was listed first, so it runs first.
    assert dependencies[0] == set()
    assert dependencies[1] == {0}
