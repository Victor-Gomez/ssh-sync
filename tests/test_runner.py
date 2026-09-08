"""Tests for the sync engine.

No backend ever runs here: `stream_command` is replaced with a stub, so these
tests cover orchestration - ordering, skipping, cancellation, event emission -
rather than rclone or robocopy behaviour.
"""

import pytest

from sshsync import runner as runner_module
from sshsync.executor import new_stats
from sshsync.runner import SyncRunner

CONFIG = {
    "servers": [
        {"name": "NAS", "host": "192.168.1.10", "user": "u", "ssh_key_path": "k"},
        {"name": "Local", "host": "localhost", "user": "u", "ssh_key_path": "k"},
    ],
    "sync_jobs": [],
}


def robocopy(name, source="C:/src", destination="D:/dst"):
    return {
        "name": name,
        "type": "robocopy",
        "source": source,
        "destination": destination,
    }


def rclone(name, server="NAS", source="C:/src", destination="/srv/dst"):
    return {
        "name": name,
        "type": "rclone",
        "server": server,
        "source": source,
        "destination": destination,
    }


@pytest.fixture
def stubs(monkeypatch):
    """Replace every side-effecting dependency and record what ran."""
    calls = {"commands": [], "stats": [], "exit_codes": {}}

    def fake_stream(
        command, on_update=None, parser_mode="rclone", on_process=None, line_buffer=None
    ):
        name = command[-1]
        calls["commands"].append(name)
        stats = new_stats()
        stats["uploaded_files"] = 3
        stats["uploaded_bytes"] = 300
        if on_update is not None:
            on_update(dict(stats))
        return calls["exit_codes"].get(name, 0), stats

    def fake_build(
        job, rclone_config_path=None, dry_run=False, transfers=None, checkers=None
    ):
        mode = "robocopy" if job["type"] == "robocopy" else "rclone"
        calls.setdefault("concurrency", []).append((transfers, checkers))
        return ["backend", "--dry" if dry_run else "--go", job["name"]], mode

    monkeypatch.setattr(runner_module, "stream_command", fake_stream)
    monkeypatch.setattr(runner_module, "build_job_command", fake_build)
    monkeypatch.setattr(runner_module, "create_temp_rclone_config", lambda s: "cfg")
    monkeypatch.setattr(runner_module, "check_server_ssh_access", lambda s: (True, ""))
    monkeypatch.setattr(runner_module, "get_directory_size_bytes", lambda p: 4096)
    # Keep tuning off the network and off the disk so orchestration tests stay
    # fast and deterministic.
    monkeypatch.setattr(runner_module, "measure_latency_ms", lambda s: 5.0)
    monkeypatch.setattr(runner_module, "profile_directory", lambda p, **kw: (3, 300))
    monkeypatch.setattr(runner_module, "write_failure_log", lambda *a: "C:/log.txt")
    monkeypatch.setattr(
        runner_module,
        "append_job_stats",
        lambda **kwargs: calls["stats"].append((kwargs["job"]["name"], kwargs["status"])),
    )
    monkeypatch.setattr(runner_module.os, "remove", lambda path: None)
    return calls


# -- happy path ------------------------------------------------------------


def test_all_jobs_run_and_succeed(stubs):
    jobs = [robocopy("A"), rclone("B")]
    result = SyncRunner(CONFIG, jobs).run()

    assert result.ok
    assert sorted(stubs["commands"]) == ["A", "B"]
    assert [state["status"] for state in result.job_states] == ["OK", "OK"]
    assert sorted(stubs["stats"]) == [("A", "OK"), ("B", "OK")]


def test_source_size_is_measured_and_recorded(stubs):
    result = SyncRunner(CONFIG, [robocopy("A")]).run()
    assert result.job_states[0]["source_bytes"] == 4096


def test_latency_is_probed_once_per_server_during_the_check(stubs, monkeypatch):
    # Two rclone jobs share the NAS: latency belongs to the reachability phase,
    # so it must be measured once up front, not once per job.
    probes = []
    monkeypatch.setattr(
        runner_module,
        "measure_latency_ms",
        lambda server: probes.append(server["name"]) or 5.0,
    )
    jobs = [rclone("A", server="NAS"), rclone("B", server="NAS")]
    SyncRunner(CONFIG, jobs).run()

    assert probes == ["NAS"]
    # Both jobs were tuned with the cached reading (5 ms -> LAN factor 1.0).
    assert stubs["concurrency"] == [(32, 32), (32, 32)]


def test_offline_server_is_not_latency_probed(stubs, monkeypatch):
    monkeypatch.setattr(
        runner_module, "check_server_ssh_access", lambda s: (False, "down")
    )
    probed = []
    monkeypatch.setattr(
        runner_module, "measure_latency_ms", lambda server: probed.append(1)
    )
    SyncRunner(CONFIG, [rclone("A", server="NAS")]).run()

    assert probed == []


def test_job_states_carry_their_selector(stubs):
    # The web UI matches live progress to its cards by selector.
    result = SyncRunner(CONFIG, [robocopy("A"), rclone("B")]).run()
    assert [state["selector"] for state in result.job_states] == [
        "robocopy:A",
        "rclone:NAS:B",
    ]


def test_dry_run_is_passed_to_the_builder(stubs, monkeypatch):
    seen = []
    monkeypatch.setattr(
        runner_module,
        "build_job_command",
        lambda job, rclone_config_path=None, dry_run=False, transfers=None, checkers=None: (
            seen.append(dry_run) or (["backend", job["name"]], "robocopy")
        ),
    )
    SyncRunner(CONFIG, [robocopy("A")], dry_run=True).run()
    assert seen == [True]


# -- failures --------------------------------------------------------------


def test_a_failing_job_is_reported_without_stopping_the_others(stubs):
    stubs["exit_codes"]["A"] = 8
    jobs = [robocopy("A"), rclone("B")]
    result = SyncRunner(CONFIG, jobs).run()

    assert not result.ok
    assert result.failures == [("A", 8)]
    assert result.job_states[0]["status"] == "FAIL"
    assert result.job_states[1]["status"] == "OK"
    assert result.log_paths == {"C:/log.txt"}
    assert "1 sync job(s) failed" in result.summary()


def test_robocopy_exit_codes_below_eight_are_successes(stubs):
    stubs["exit_codes"]["A"] = 3
    result = SyncRunner(CONFIG, [robocopy("A")]).run()
    assert result.ok


def test_rclone_nonzero_exit_is_a_failure(stubs):
    stubs["exit_codes"]["B"] = 1
    result = SyncRunner(CONFIG, [rclone("B")]).run()
    assert not result.ok


# -- offline servers -------------------------------------------------------


def test_jobs_on_an_offline_server_are_skipped(stubs, monkeypatch):
    monkeypatch.setattr(
        runner_module, "check_server_ssh_access", lambda s: (False, "no route")
    )
    jobs = [robocopy("A"), rclone("B")]
    result = SyncRunner(CONFIG, jobs).run()

    assert stubs["commands"] == ["A"]
    assert result.job_states[1]["status"] == "SKIPPED"
    assert result.offline_servers == {"NAS": "no route"}
    assert result.ok  # Skipping is not a failure.


def test_an_all_offline_run_finishes_immediately(stubs, monkeypatch):
    monkeypatch.setattr(
        runner_module, "check_server_ssh_access", lambda s: (False, "down")
    )
    result = SyncRunner(CONFIG, [rclone("B")]).run()

    assert stubs["commands"] == []
    assert result.job_states[0]["status"] == "SKIPPED"


def test_unknown_server_raises_before_anything_runs(stubs):
    with pytest.raises(RuntimeError, match="Unknown server 'Ghost'"):
        SyncRunner(CONFIG, [rclone("B", server="Ghost")]).run()
    assert stubs["commands"] == []


# -- ordering --------------------------------------------------------------


def test_dependent_jobs_keep_their_config_order(stubs):
    # The rclone job uploads the tree the robocopy job writes.
    jobs = [
        robocopy("Mirror", source="C:/Docs", destination="D:/Backup"),
        rclone("Upload", source="D:/Backup", destination="/srv/Docs"),
    ]
    SyncRunner(CONFIG, jobs).run()

    assert stubs["commands"] == ["Mirror", "Upload"]


def test_same_lane_jobs_run_in_order(stubs):
    jobs = [robocopy("A"), robocopy("B"), robocopy("C")]
    SyncRunner(CONFIG, jobs).run()

    assert stubs["commands"] == ["A", "B", "C"]


# -- cancellation ----------------------------------------------------------


def test_cancelling_mid_run_marks_the_job_cancelled(stubs, monkeypatch):
    holder = {}

    def cancelling_stream(
        command, on_update=None, parser_mode="rclone", on_process=None, line_buffer=None
    ):
        holder["runner"].cancel()
        return 0, new_stats()

    monkeypatch.setattr(runner_module, "stream_command", cancelling_stream)
    runner = SyncRunner(CONFIG, [robocopy("A"), robocopy("B")])
    holder["runner"] = runner
    result = runner.run()

    assert result.job_states[0]["status"] == "CANCELLED"
    assert result.job_states[1]["status"] == "PENDING"


def test_cancel_terminates_live_processes(stubs):
    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    process = FakeProcess()
    runner = SyncRunner(CONFIG, [robocopy("A")])
    runner._track_process(process, True)
    runner.cancel()

    assert process.terminated


# -- events ----------------------------------------------------------------


def test_events_describe_the_whole_run(stubs):
    events = []
    SyncRunner(CONFIG, [robocopy("A")], on_event=events.append).run()
    types = [event["type"] for event in events]

    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    assert "job_updated" in types
    assert events[-1]["result"]["ok"] is True


def test_a_broken_listener_cannot_break_the_run(stubs):
    def explode(event):
        raise ValueError("listener is broken")

    result = SyncRunner(CONFIG, [robocopy("A")], on_event=explode).run()
    assert result.ok


def test_result_serializes_to_json_friendly_data(stubs):
    stubs["exit_codes"]["A"] = 8
    payload = SyncRunner(CONFIG, [robocopy("A")]).run().to_dict()

    assert payload["ok"] is False
    assert payload["failures"] == [{"name": "A", "exit_code": 8}]
    assert payload["jobs"][0]["name"] == "A"
    assert payload["log_paths"] == ["C:/log.txt"]
