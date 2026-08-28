"""Tests for failure log locations and the dated log index."""

import pytest

from sshsync import logs as logs_module
from sshsync.logs import available_log_dates, log_path_for_date, write_failure_log


@pytest.fixture(autouse=True)
def isolated_log_dir(tmp_path, monkeypatch):
    """Point the logger at a temp directory so tests never touch real logs."""
    monkeypatch.setattr(logs_module, "LOG_DIR", tmp_path / "logs")
    return tmp_path / "logs"


def test_path_is_built_from_an_iso_date(isolated_log_dir):
    assert log_path_for_date("2026-04-01") == isolated_log_dir / "sync-2026-04-01.log"


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "2026-4-1",
        "01-04-2026",
        "2026-13-01",  # Well-formed but not a real month.
        "../../secrets",
        "2026-04-01/../../etc/passwd",
    ],
)
def test_bad_dates_are_refused(value):
    # The date arrives as a query parameter, so nothing that could address a
    # file outside the log directory may be turned into a path.
    with pytest.raises(ValueError):
        log_path_for_date(value)


def test_available_dates_are_newest_first(isolated_log_dir):
    isolated_log_dir.mkdir(parents=True)
    for name in ("sync-2026-04-01.log", "sync-2026-04-03.log", "sync-2026-04-02.log"):
        (isolated_log_dir / name).write_text("x", encoding="utf-8")

    assert available_log_dates() == ["2026-04-03", "2026-04-02", "2026-04-01"]


def test_available_dates_ignore_unrelated_files(isolated_log_dir):
    isolated_log_dir.mkdir(parents=True)
    (isolated_log_dir / "sync-2026-04-01.log").write_text("x", encoding="utf-8")
    (isolated_log_dir / "sync-notes.log").write_text("x", encoding="utf-8")
    (isolated_log_dir / "other.txt").write_text("x", encoding="utf-8")

    assert available_log_dates() == ["2026-04-01"]


def test_missing_log_dir_has_no_dates():
    assert available_log_dates() == []


def test_written_failures_land_on_a_listed_date(isolated_log_dir):
    path = write_failure_log(
        job_name="Docs",
        server="NAS",
        command=["rclone", "sync"],
        exit_code=1,
        stats={"error_count": 2, "last_error": "boom"},
        tail_lines=["something failed"],
    )

    assert path is not None
    assert path.name[len("sync-") : -len(".log")] in available_log_dates()
