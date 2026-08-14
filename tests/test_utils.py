"""Tests for path handling and formatting helpers."""

import pytest

from sshsync.utils import (
    format_bytes,
    format_elapsed,
    get_directory_size_bytes,
    is_failed_exit_code,
    normalize_remote_path,
    parse_elapsed_seconds,
    require_job_value,
)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (1024**3, "1.00 GiB"),
        (1024**5 * 3, "3.00 PiB"),
    ],
)
def test_format_bytes(size, expected):
    assert format_bytes(size) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (90, "1m30s"), (3661, "1h01m01s"), (-5, "0s")],
)
def test_format_elapsed(seconds, expected):
    assert format_elapsed(seconds) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0s", 0), ("45s", 45), ("1m30s", 90), ("1h01m01s", 3661), ("", 0), ("nope", 0)],
)
def test_parse_elapsed_seconds(text, expected):
    assert parse_elapsed_seconds(text) == expected


def test_elapsed_round_trips():
    for seconds in (0, 7, 59, 60, 3599, 3600, 86399):
        assert parse_elapsed_seconds(format_elapsed(seconds)) == seconds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D:/Fotos", "/D:/Fotos"),
        ("D:\\Fotos\\", "/D:/Fotos"),
        ("/srv/backup/", "/srv/backup"),
        ("/D:/Already", "/D:/Already"),
    ],
)
def test_normalize_remote_path(raw, expected):
    assert normalize_remote_path(raw) == expected


def test_require_job_value_returns_value():
    assert require_job_value({"source": "C:/x"}, "source", "robocopy") == "C:/x"


@pytest.mark.parametrize("job", [{}, {"source": ""}, {"source": "   "}])
def test_require_job_value_rejects_missing(job):
    with pytest.raises(RuntimeError, match="Missing required key 'source'"):
        require_job_value(job, "source", "robocopy")


@pytest.mark.parametrize(
    ("job_type", "exit_code", "failed"),
    [
        ("robocopy", 0, False),
        ("robocopy", 3, False),  # Files copied and extras removed: success.
        ("robocopy", 7, False),
        ("robocopy", 8, True),
        ("robocopy", 16, True),
        ("rclone", 0, False),
        ("rclone", 1, True),
    ],
)
def test_is_failed_exit_code(job_type, exit_code, failed):
    assert is_failed_exit_code(job_type, exit_code) is failed


def test_directory_size_sums_nested_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    nested = tmp_path / "nested" / "deep"
    nested.mkdir(parents=True)
    (nested / "b.bin").write_bytes(b"y" * 250)

    assert get_directory_size_bytes(tmp_path) == 350


def test_directory_size_tolerates_missing_path(tmp_path):
    assert get_directory_size_bytes(tmp_path / "does-not-exist") == 0
