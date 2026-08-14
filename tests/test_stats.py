"""Tests for the append-only run history and its roll-ups."""

import json

import pytest

from sshsync import stats as stats_module
from sshsync.stats import append_job_stats, load_entries, summarize


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the history at a temp file so tests never touch real data."""
    monkeypatch.setattr(stats_module, "STATS_PATH", tmp_path / "sync-stats.jsonl")
    monkeypatch.setattr(stats_module, "LEGACY_STATS_PATH", tmp_path / "sync-stats.json")
    return tmp_path


def record(name="Docs", job_type="rclone", server="NAS", status="OK", **overrides):
    """Append one history entry with sensible defaults."""
    job = {
        "name": name,
        "type": job_type,
        "server": server,
        "source": "C:/x",
        "destination": "/srv/x",
    }
    payload = {
        "uploaded_files": 10,
        "uploaded_bytes": 1024,
        "deleted_files": 2,
        "deleted_bytes": 512,
        "checks_done": 50,
        "checks_total": 50,
        "elapsed": "1m00s",
        "error_count": 0,
        "last_error": "",
    }
    payload.update(overrides.pop("stats", {}))
    return append_job_stats(
        job=job,
        stats=payload,
        status=status,
        exit_code=overrides.pop("exit_code", 0),
        source_bytes=overrides.pop("source_bytes", 4096),
        dry_run=overrides.pop("dry_run", False),
    )


def test_empty_history_reads_as_empty():
    assert load_entries() == []
    assert summarize()["totals"]["runs"] == 0


def test_entries_round_trip():
    record()
    entries = load_entries()

    assert len(entries) == 1
    assert entries[0]["name"] == "Docs"
    assert entries[0]["stats"]["uploaded_bytes"] == 1024
    assert entries[0]["ended_at"].endswith("+00:00")


def test_appends_are_one_line_each(isolated_store):
    for _ in range(3):
        record()
    lines = (isolated_store / "sync-stats.jsonl").read_text(encoding="utf-8").strip()

    assert len(lines.splitlines()) == 3
    assert all(json.loads(line) for line in lines.splitlines())


def test_limit_returns_the_most_recent_entries():
    for index in range(5):
        record(name=f"Job{index}")
    assert [entry["name"] for entry in load_entries(limit=2)] == ["Job3", "Job4"]


def test_corrupt_lines_are_skipped(isolated_store):
    record()
    with (isolated_store / "sync-stats.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{ truncated\n")
    record()

    assert len(load_entries()) == 2


def test_robocopy_entries_are_recorded_as_local():
    record(job_type="robocopy", server="ignored")
    assert load_entries()[0]["server"] == "local"


def test_summary_aggregates_across_views():
    record(name="Docs", server="NAS")
    record(name="Docs", server="NAS", status="FAIL", stats={"error_count": 3})
    record(name="Photos", server="Local")

    summary = summarize()
    totals = summary["totals"]

    assert totals["runs"] == 3
    assert totals["failures"] == 1
    assert totals["uploaded_bytes"] == 3072
    assert totals["error_count"] == 3
    assert totals["elapsed_seconds"] == 180

    by_name = {job["name"]: job for job in summary["jobs"]}
    assert by_name["Docs"]["runs"] == 2
    assert by_name["Docs"]["failures"] == 1
    assert by_name["Photos"]["runs"] == 1

    by_server = {server["server"]: server for server in summary["servers"]}
    assert by_server["NAS"]["runs"] == 2
    assert by_server["Local"]["runs"] == 1


def test_summary_tracks_the_latest_run_timestamp():
    record()
    record()
    summary = summarize()

    assert summary["totals"]["last_ended_at"] == load_entries()[-1]["ended_at"]


def test_legacy_store_is_migrated_once(isolated_store):
    legacy = isolated_store / "sync-stats.json"
    legacy.write_text(
        json.dumps({"jobs": [{"name": "Old", "stats": {"uploaded_files": 1}}]}),
        encoding="utf-8",
    )

    entries = load_entries()

    assert [entry["name"] for entry in entries] == ["Old"]
    assert not legacy.exists()
    assert (isolated_store / "sync-stats.json.migrated").is_file()
