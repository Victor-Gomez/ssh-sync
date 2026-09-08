"""Tests for the web API.

The runner is stubbed out, so these cover the HTTP surface: config editing,
run control and history endpoints.
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from sshsync import config as config_module
from sshsync import logs as logs_module
from sshsync import stats as stats_module
from sshsync.config import job_selector
from sshsync.web import app as app_module
from sshsync.web import manager as manager_module
from sshsync.web.app import create_app
from sshsync.web.manager import RunManager


@pytest.fixture
def client(tmp_path, config_file, monkeypatch):
    """A test client whose config and history live in tmp_path."""
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(app_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(stats_module, "STATS_PATH", tmp_path / "stats.jsonl")
    monkeypatch.setattr(stats_module, "LEGACY_STATS_PATH", tmp_path / "stats.json")
    with TestClient(create_app()) as test_client:
        yield test_client


# -- config ----------------------------------------------------------------


def test_get_config_returns_jobs_and_servers(client):
    payload = client.get("/api/config").json()

    assert len(payload["servers"]) == 2
    assert len(payload["jobs"]) == 4
    assert payload["jobs"][0]["selector"] == "robocopy:Docs"
    assert payload["jobs"][3]["enabled"] is False


def test_job_summaries_carry_their_config_index(client):
    # The editor writes an edit back by index, so it must survive reordering.
    jobs = client.get("/api/config").json()["jobs"]
    assert [job["index"] for job in jobs] == [0, 1, 2, 3]


def test_put_config_persists_changes(client, config_file, sample_config):
    sample_config["sync_jobs"][0]["destination"] = "E:/Elsewhere"
    response = client.put("/api/config", json=sample_config)

    assert response.status_code == 200
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["sync_jobs"][0]["destination"] == "E:/Elsewhere"


def test_put_config_rejects_invalid_payloads(client, config_file, sample_config):
    original = config_file.read_text(encoding="utf-8")
    sample_config["sync_jobs"][1]["server"] = "Ghost"
    response = client.put("/api/config", json=sample_config)

    assert response.status_code == 422
    assert "Ghost" in response.json()["detail"]
    assert config_file.read_text(encoding="utf-8") == original


def test_validate_endpoint_reports_without_saving(client, sample_config):
    assert client.post("/api/config/validate", json=sample_config).json()["valid"]

    sample_config["sync_jobs"][0]["type"] = "rsync"
    result = client.post("/api/config/validate", json=sample_config).json()
    assert result["valid"] is False
    assert "rsync" in result["error"]


# -- command preview -------------------------------------------------------


def test_preview_returns_the_backend_command(client, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "build_job_command",
        lambda job, **kw: (["robocopy", "a", "b"], "robocopy"),
    )
    payload = client.post("/api/jobs/preview", json={"selector": "robocopy:Docs"}).json()

    assert payload["parser"] == "robocopy"
    assert payload["command"][0] == "robocopy"


def test_preview_rejects_unknown_jobs(client):
    response = client.post("/api/jobs/preview", json={"selector": "robocopy:Nope"})
    assert response.status_code == 404


# -- runs ------------------------------------------------------------------


def test_status_starts_idle(client):
    status = client.get("/api/status").json()
    assert status["running"] is False
    assert status["jobs"] == []


def test_run_submits_a_single_request(client, monkeypatch):
    submitted = {}

    def fake_submit(self, items):
        submitted["items"] = items
        return {"running": True, "jobs": [], "queue": []}

    monkeypatch.setattr(manager_module.RunManager, "submit", fake_submit)
    response = client.post(
        "/api/run", json={"selectors": ["robocopy:Docs"], "dry_run": True}
    )

    assert response.status_code == 200
    assert submitted["items"] == [{"selectors": ["robocopy:Docs"], "dry_run": True}]


def test_run_accepts_a_batch_of_requests(client, monkeypatch):
    submitted = {}

    def fake_submit(self, items):
        submitted["items"] = items
        return {"running": True, "queue": []}

    monkeypatch.setattr(manager_module.RunManager, "submit", fake_submit)
    client.post(
        "/api/run",
        json={
            "items": [
                {"selectors": ["robocopy:Docs"]},
                {"selectors": ["rclone:NAS:Docs"], "dry_run": True},
            ]
        },
    )

    assert submitted["items"] == [
        {"selectors": ["robocopy:Docs"], "dry_run": False},
        {"selectors": ["rclone:NAS:Docs"], "dry_run": True},
    ]


def test_run_reports_bad_selectors(client):
    response = client.post("/api/run", json={"selectors": ["Nope"]})
    assert response.status_code == 422


def test_queue_remove_requires_an_integer_id(client):
    assert client.post("/api/queue/remove", json={}).status_code == 400
    assert client.post("/api/queue/remove", json={"id": "5"}).status_code == 400


def test_queue_remove_calls_dequeue(client, monkeypatch):
    removed = {}

    def fake_dequeue(self, entry_id):
        removed["id"] = entry_id
        return {"queue": []}

    monkeypatch.setattr(manager_module.RunManager, "dequeue", fake_dequeue)
    response = client.post("/api/queue/remove", json={"id": 7})

    assert response.status_code == 200
    assert removed["id"] == 7


# -- run queue (manager behaviour) -----------------------------------------


class _FakeResult:
    def __init__(self, jobs):
        self._jobs = jobs

    def to_dict(self):
        return {"ok": True, "summary": "", "jobs": self._jobs, "log_paths": []}


class _FakeRunner:
    """A runner that blocks in `run()` until released, so a test can control
    exactly when one run ends and the next is promoted."""

    def __init__(self, config, jobs, dry_run=False, on_event=None):
        self.jobs = list(jobs)
        self.dry_run = dry_run
        self.job_states = [{"selector": job_selector(job)} for job in self.jobs]
        self.elapsed_text = "0s"
        self._gate = threading.Event()

    def run(self):
        self._gate.wait(5)
        return _FakeResult(self.job_states)

    def cancel(self):
        self._gate.set()


def _running_selector(manager):
    jobs = manager.state["jobs"]
    return jobs[0]["selector"] if jobs else None


def _wait_until(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def queue_manager(config_file, monkeypatch):
    """A RunManager whose runs are fake and instantly controllable."""
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(manager_module, "SyncRunner", _FakeRunner)
    manager = RunManager()
    yield manager
    # Release any run still blocked in its gate so the daemon thread can exit.
    if manager._runner is not None:
        manager._runner.cancel()


def test_first_request_runs_and_the_rest_queue(queue_manager):
    state = queue_manager.submit([{"selectors": ["robocopy:Docs"], "dry_run": False}])
    assert state["running"] is True
    assert state["queue"] == []
    assert _running_selector(queue_manager) == "robocopy:Docs"

    state = queue_manager.submit([{"selectors": ["rclone:NAS:Docs"], "dry_run": False}])
    assert state["running"] is True
    assert [entry["selectors"] for entry in state["queue"]] == [["rclone:NAS:Docs"]]


def test_a_job_cannot_be_queued_twice_or_while_running(queue_manager):
    queue_manager.submit([{"selectors": ["robocopy:Docs"]}])  # now running
    queue_manager.submit([{"selectors": ["rclone:NAS:Docs"]}])  # queued

    # The running job cannot also be queued behind itself.
    state = queue_manager.submit([{"selectors": ["robocopy:Docs"]}])
    assert len(state["queue"]) == 1

    # Nor can an already-queued job be queued a second time.
    state = queue_manager.submit([{"selectors": ["rclone:NAS:Docs"]}])
    assert len(state["queue"]) == 1


def test_finishing_a_run_promotes_the_next_queued_job(queue_manager):
    queue_manager.submit([{"selectors": ["robocopy:Docs"]}])
    queue_manager.submit([{"selectors": ["rclone:NAS:Docs"]}])

    # End the first run; the queued job should start on its own.
    queue_manager._runner.cancel()

    assert _wait_until(lambda: not queue_manager.state["queue"])
    assert _wait_until(lambda: queue_manager.state["running"])
    assert _running_selector(queue_manager) == "rclone:NAS:Docs"


def test_a_batch_starts_one_and_queues_the_rest_in_order(queue_manager):
    state = queue_manager.submit(
        [
            {"selectors": ["robocopy:Docs"]},
            {"selectors": ["rclone:NAS:Docs"]},
            {"selectors": ["rclone:Local:Docs"]},
        ]
    )
    assert _running_selector(queue_manager) == "robocopy:Docs"
    assert [entry["selectors"] for entry in state["queue"]] == [
        ["rclone:NAS:Docs"],
        ["rclone:Local:Docs"],
    ]


def test_dequeue_removes_a_waiting_job(queue_manager):
    queue_manager.submit([{"selectors": ["robocopy:Docs"]}])
    state = queue_manager.submit([{"selectors": ["rclone:NAS:Docs"]}])
    entry_id = state["queue"][0]["id"]

    state = queue_manager.dequeue(entry_id)
    assert state["queue"] == []


def test_cancel_clears_the_queue(queue_manager):
    queue_manager.submit([{"selectors": ["robocopy:Docs"]}])
    queue_manager.submit([{"selectors": ["rclone:NAS:Docs"]}])

    queue_manager.cancel()

    assert queue_manager.state["queue"] == []
    # The cancelled run ends and, with an empty queue, nothing is promoted.
    assert _wait_until(lambda: not queue_manager.state["running"])


def test_server_check_can_target_one_server(client, monkeypatch):
    probed = []

    def fake_check(server, timeout_seconds=4):
        probed.append(server["name"])
        return True, ""

    monkeypatch.setattr(app_module, "check_server_ssh_access", fake_check)
    payload = client.post("/api/servers/check", json={"names": ["Local"]}).json()

    assert probed == ["Local"]
    assert [server["name"] for server in payload["servers"]] == ["Local"]


def test_server_check_without_names_probes_all(client, monkeypatch):
    monkeypatch.setattr(app_module, "check_server_ssh_access", lambda s, **kw: (True, ""))
    payload = client.post("/api/servers/check", json={}).json()

    assert [server["name"] for server in payload["servers"]] == ["NAS", "Local"]


def test_server_check_rejects_an_unknown_name(client):
    response = client.post("/api/servers/check", json={"names": ["Ghost"]})
    assert response.status_code == 404


def test_cancel_without_a_run_conflicts(client):
    response = client.post("/api/cancel")
    assert response.status_code == 409


# -- history and logs ------------------------------------------------------


def test_history_is_empty_before_any_run(client):
    assert client.get("/api/history").json()["entries"] == []
    assert client.get("/api/history/summary").json()["totals"]["runs"] == 0


def test_history_returns_newest_first(client):
    job = {"name": "Docs", "type": "rclone", "server": "NAS"}
    for index in range(3):
        stats_module.append_job_stats(
            job={**job, "name": f"Job{index}"},
            stats={"uploaded_files": index},
            status="OK",
            exit_code=0,
        )

    entries = client.get("/api/history").json()["entries"]
    assert [entry["name"] for entry in entries] == ["Job2", "Job1", "Job0"]


def test_logs_endpoint_handles_a_missing_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "log_path_for_today", lambda: tmp_path / "none.log")
    payload = client.get("/api/logs").json()

    assert payload["exists"] is False
    assert payload["lines"] == []


def test_logs_endpoint_tails_the_file(client, monkeypatch, tmp_path):
    log = tmp_path / "sync.log"
    log.write_text("\n".join(f"line {index}" for index in range(1000)), encoding="utf-8")
    monkeypatch.setattr(app_module, "log_path_for_today", lambda: log)

    payload = client.get("/api/logs").json()
    assert len(payload["lines"]) == app_module.LOG_TAIL_LIMIT
    assert payload["lines"][-1] == "line 999"


# -- live feed -------------------------------------------------------------


def test_websocket_opens_with_a_snapshot(client):
    with client.websocket_connect("/ws") as socket:
        message = socket.receive_json()

    assert message["type"] == "snapshot"
    assert message["state"]["running"] is False


# -- static ----------------------------------------------------------------


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "SSH-Sync" in response.text


def test_index_page_is_revalidated(client):
    # A cached shell paired with fresh /static assets renders a broken page.
    assert client.get("/").headers["cache-control"] == "no-cache"


# -- command preview of an unsaved job -------------------------------------


def test_preview_accepts_a_job_that_is_not_saved_yet(client):
    # The editor previews what is in its fields, which may never have been saved.
    draft = {
        "name": "Draft",
        "type": "robocopy",
        "source": "C:/Draft",
        "destination": "D:/Backup/Draft",
        "blacklisted_dirnames": ["scratch"],
    }
    payload = client.post("/api/jobs/preview", json={"job": draft}).json()

    assert payload["parser"] == "robocopy"
    assert payload["selector"] == "robocopy:Draft"
    assert "C:/Draft" in payload["command"]
    assert "scratch" in payload["command"]


def test_preview_of_a_draft_honours_dry_run(client):
    draft = {
        "name": "Draft",
        "type": "robocopy",
        "source": "C:/Draft",
        "destination": "D:/Backup/Draft",
    }
    payload = client.post(
        "/api/jobs/preview", json={"job": draft, "dry_run": True}
    ).json()

    assert "/L" in payload["command"]


def test_preview_rejects_an_invalid_draft(client):
    draft = {"name": "Draft", "type": "robocopy", "source": "", "destination": "D:/b"}
    response = client.post("/api/jobs/preview", json={"job": draft})

    assert response.status_code == 422
    assert "source" in response.json()["detail"]


def test_preview_rejects_a_draft_naming_an_unknown_server(client):
    draft = {
        "name": "Draft",
        "type": "rclone",
        "server": "Ghost",
        "source": "C:/Draft",
        "destination": "/srv/draft",
    }
    response = client.post("/api/jobs/preview", json={"job": draft})

    assert response.status_code == 422
    assert "Ghost" in response.json()["detail"]


# -- dated failure logs ----------------------------------------------------


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Point the log helpers at a temp directory holding two days of logs."""
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(logs_module, "LOG_DIR", directory)
    (directory / "sync-2026-04-01.log").write_text("older failure", encoding="utf-8")
    (directory / "sync-2026-04-02.log").write_text("newer failure", encoding="utf-8")
    return directory


def test_log_dates_are_listed_newest_first(client, log_dir):
    payload = client.get("/api/logs/dates").json()

    assert payload["dates"][:2] == ["2026-04-02", "2026-04-01"]
    assert payload["today"] == logs_module.today_text()


def test_logs_can_be_read_for_an_earlier_day(client, log_dir):
    payload = client.get("/api/logs", params={"date": "2026-04-01"}).json()

    assert payload["exists"] is True
    assert payload["date"] == "2026-04-01"
    assert payload["lines"] == ["older failure"]


def test_logs_default_to_today(client, log_dir):
    payload = client.get("/api/logs").json()

    assert payload["date"] == logs_module.today_text()
    assert payload["exists"] is False


def test_logs_reject_a_malformed_date(client, log_dir):
    response = client.get("/api/logs", params={"date": "01-04-2026"})

    assert response.status_code == 400


def test_logs_reject_a_date_that_escapes_the_log_directory(client, log_dir):
    # A date is the only thing that may become a path here.
    response = client.get("/api/logs", params={"date": "../../../../etc/passwd"})

    assert response.status_code == 400
