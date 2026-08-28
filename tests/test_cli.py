"""Tests for command-line argument handling and the read-only commands."""

import json

import pytest
from rich.console import Console

from sshsync import cli
from sshsync import config as config_module
from sshsync import stats as stats_module
from sshsync.cli import EXIT_OK, main, parse_args
from sshsync.stats import format_ended_at


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Keep the history out of the way of the real one."""
    monkeypatch.setattr(stats_module, "STATS_PATH", tmp_path / "sync-stats.jsonl")
    monkeypatch.setattr(stats_module, "LEGACY_STATS_PATH", tmp_path / "sync-stats.json")


def run_main(argv, capsys):
    """Run the CLI and return its exit code plus everything it printed."""
    code = main(argv)
    return code, capsys.readouterr().out


# -- parsing ---------------------------------------------------------------


def test_list_and_config_default_to_off():
    args = parse_args([])
    assert args.list_jobs is False
    assert args.config is None


def test_config_takes_a_path():
    assert parse_args(["--config", "other.json"]).config == "other.json"


# -- --list ----------------------------------------------------------------


def test_list_prints_a_selector_for_every_job(config_file, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_file)

    code, output = run_main(["--list"], capsys)

    assert code == EXIT_OK
    # Rich wraps to the terminal width, so match on the fragments that survive.
    assert "robocopy:Docs" in output.replace("\n", "")
    assert "rclone:NAS:Docs" in output.replace("\n", "")


def test_list_includes_disabled_jobs(config_file, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_file)

    _, output = run_main(["--list"], capsys)

    # Disabled jobs still need a selector: running one explicitly is the point.
    assert "robocopy:Archive" in output.replace("\n", "")


def test_list_runs_nothing(config_file, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_file)
    monkeypatch.setattr(
        cli, "SyncRunner", lambda *a, **k: pytest.fail("--list must not start a run")
    )

    assert run_main(["--list"], capsys)[0] == EXIT_OK


def test_list_shows_the_last_run_of_a_job(config_file, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    entry = stats_module.append_job_stats(
        job={"name": "Docs", "type": "rclone", "server": "NAS"},
        stats={"elapsed": "5s"},
        status="OK",
        exit_code=0,
    )

    console = Console(width=200, record=True)
    cli.print_job_list(config_module.load_config(config_file), config_file, console)
    output = console.export_text()

    assert format_ended_at(entry["ended_at"]) in output  # The job that has run...
    assert "never" in output  # ...and the ones that have not.


# -- --config --------------------------------------------------------------


def test_config_flag_reads_the_named_file(tmp_path, sample_config, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere.json"
    sample_config["sync_jobs"] = [
        {
            "name": "OnlyHere",
            "type": "robocopy",
            "source": "C:/a",
            "destination": "D:/b",
        }
    ]
    elsewhere.write_text(json.dumps(sample_config), encoding="utf-8")
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "not-this-one.json")

    _, output = run_main(["--config", str(elsewhere), "--list"], capsys)

    assert "robocopy:OnlyHere" in output.replace("\n", "")
    assert str(elsewhere.name) in output.replace("\n", "")


def test_missing_config_file_is_reported(tmp_path, capsys):
    with pytest.raises(Exception, match="Missing config file"):
        main(["--config", str(tmp_path / "absent.json"), "--list"])


def test_print_job_list_needs_no_history(config_file, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_file)
    config = config_module.load_config(config_file)

    # A fresh install has no stats file at all; listing must still work.
    cli.print_job_list(config, config_file, Console(width=200))
