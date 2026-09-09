"""Tests for backend output parsing.

The parsers are the part of the tool most likely to break silently when a
backend changes its output, so they are exercised against real captured lines.
"""

import pytest

from sshsync.executor import (
    RcloneParser,
    RobocopyParser,
    _is_error_line,
    new_stats,
    parse_robocopy_size,
    parse_size,
)


def feed_all(parser, lines):
    """Run a parser over a block of output and return the resulting stats."""
    stats = new_stats()
    for line in lines.strip().splitlines():
        parser.feed(stats, line.strip())
    return stats


# -- size parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512 B", 512),
        ("1 KiB", 1024),
        ("1.5 MiB", 1572864),
        ("2 GiB", 2147483648),
        ("1 kB", 1000),
        ("1,024 B", 1024),
        ("garbage", 0),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1024", 1024), ("1 k", 1024), ("2.5 m", 2621440), ("1 g", 1073741824), ("", 0)],
)
def test_parse_robocopy_size(text, expected):
    assert parse_robocopy_size(text) == expected


# -- rclone ----------------------------------------------------------------

RCLONE_OUTPUT = """
Transferred:        1.234 GiB / 1.234 GiB, 100%, 12.345 MiB/s, ETA 0s
Checks:               842 / 900, 94%
Deleted:               12 (files), 3 (dirs), 4.500 MiB (freed)
Transferred:           57 / 57, 100%
Elapsed time:       2m30.1s
"""


def test_rclone_parses_a_full_stats_block():
    stats = feed_all(RcloneParser(), RCLONE_OUTPUT)

    assert stats["uploaded_bytes"] == int(1.234 * 1024**3)
    assert stats["uploaded_files"] == 57
    assert stats["deleted_files"] == 12
    assert stats["deleted_bytes"] == int(4.5 * 1024**2)
    assert stats["checks_done"] == 842
    assert stats["checks_total"] == 900
    assert stats["elapsed"] == "2m30.1s"
    assert stats["error_count"] == 0


def test_rclone_counts_errors_and_keeps_the_latest():
    stats = feed_all(
        RcloneParser(),
        """
        2024/01/01 10:00:00 ERROR : file1.txt: failed to copy: permission denied
        2024/01/01 10:00:01 ERROR : file2.txt: failed to copy: disk full
        """,
    )
    assert stats["error_count"] == 2
    assert stats["last_error"] == "file2.txt: failed to copy: disk full"


def test_rclone_ignores_unrelated_lines():
    stats = feed_all(RcloneParser(), "NOTICE: something happened\nrandom text")
    assert stats == new_stats()


def test_rclone_finalize_leaves_stats_untouched():
    parser = RcloneParser()
    stats = feed_all(parser, RCLONE_OUTPUT)
    before = dict(stats)
    parser.finalize(stats, 0)
    assert stats == before


# -- robocopy --------------------------------------------------------------

ROBOCOPY_OUTPUT = """
               Total    Copied   Skipped  Mismatch    FAILED    Extras
    Dirs :        45        2        43         0         0         1
   Files :       900       57       840         0         3        12
   Bytes :   1.234 g   45.6 m   1.18 g         0         0   4.50 m
"""


def test_robocopy_parses_the_summary_table():
    stats = feed_all(RobocopyParser(), ROBOCOPY_OUTPUT)

    assert stats["uploaded_files"] == 57
    assert stats["deleted_files"] == 12
    assert stats["checks_total"] == 900
    assert stats["checks_done"] == 900  # 57 copied + 840 skipped + 3 failed
    assert stats["error_count"] == 3
    assert stats["uploaded_bytes"] == int(45.6 * 1024**2)
    assert stats["deleted_bytes"] == int(4.5 * 1024**2)


def test_robocopy_clears_transient_errors_on_success():
    parser = RobocopyParser()
    stats = feed_all(parser, "ERROR 32 (0x00000020) Copying File locked.txt")
    parser.finalize(stats, 1)  # Exit 1: files copied, no failure.

    assert stats["error_count"] == 0
    assert stats["last_error"] == ""


def test_robocopy_surfaces_the_last_error_on_failure():
    parser = RobocopyParser()
    stats = feed_all(parser, "ERROR 5 (0x00000005) Accessing Destination Access denied")
    parser.finalize(stats, 8)

    assert stats["error_count"] == 1
    assert "Access denied" in stats["last_error"]


def test_robocopy_synthesizes_an_error_when_none_was_printed():
    parser = RobocopyParser()
    stats = new_stats()
    parser.finalize(stats, 16)

    assert stats["error_count"] == 1
    assert stats["last_error"] == "Robocopy failed with exit code 16"


def test_checks_done_never_exceeds_total():
    stats = feed_all(RobocopyParser(), "   Files :  10  8  8  0  0  0")
    assert stats["checks_done"] == 10


# -- error line detection --------------------------------------------------


def test_rclone_error_line_is_detected():
    line = (
        "2026/09/03 13:28:18 ERROR : photo.jpg: Couldn't delete: "
        "remove /D:/Fotos/zTools/photo.jpg: permission denied"
    )
    assert _is_error_line(line, "rclone")


@pytest.mark.parametrize(
    "line",
    [
        "Transferred:   	    2.071 GiB / 2.071 GiB, 100%",
        "2026/09/03 13:28:18 NOTICE: some notice",
        # The word "error" in a path must not be mistaken for an error tag.
        "2026/09/03 13:28:18 INFO  : error-report.txt: Copied (new)",
    ],
)
def test_rclone_non_error_lines_are_ignored(line):
    assert not _is_error_line(line, "rclone")


def test_robocopy_error_line_is_detected():
    assert _is_error_line("2026/09/03 ERROR 32 (0x00000020) Copying File", "robocopy")
    assert not _is_error_line("   Files :  10  8  8  0  0  0", "robocopy")
