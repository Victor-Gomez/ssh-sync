"""Tests for config validation, persistence and job selection."""

import json

import pytest

from sshsync.config import (
    ConfigError,
    job_selector,
    load_config,
    parse_selector,
    save_config,
    select_jobs,
    validate_config,
)

# -- validation ------------------------------------------------------------


def test_valid_config_passes(sample_config):
    assert validate_config(sample_config) is sample_config


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c.pop("servers"), "'servers' must be an array"),
        (lambda c: c.update(sync_jobs=[]), "must be a non-empty array"),
        (lambda c: c["servers"][0].pop("host"), "missing required field 'host'"),
        (lambda c: c["servers"][0].pop("name"), "must have a 'name' field"),
        (lambda c: c["sync_jobs"][0].update(type="rsync"), "invalid type 'rsync'"),
        (lambda c: c["sync_jobs"][1].update(server="Ghost"), "unknown server 'Ghost'"),
        (lambda c: c["sync_jobs"][0].update(enabled="yes"), "must be true or false"),
        (lambda c: c["sync_jobs"][0].update(source=""), "non-empty 'source'"),
    ],
)
def test_invalid_configs_are_rejected(sample_config, mutate, message):
    mutate(sample_config)
    with pytest.raises(ConfigError, match=message):
        validate_config(sample_config)


def test_duplicate_server_names_rejected(sample_config):
    sample_config["servers"].append(dict(sample_config["servers"][0]))
    with pytest.raises(ConfigError, match="Duplicate server name"):
        validate_config(sample_config)


def test_load_config_reports_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="Missing config file"):
        load_config(tmp_path / "nope.json")


def test_load_config_reports_bad_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="Could not load"):
        load_config(path)


def test_load_config_reads_valid_file(config_file, sample_config):
    assert load_config(config_file) == sample_config


# -- persistence -----------------------------------------------------------


def test_save_config_round_trips(tmp_path, sample_config):
    path = tmp_path / "out.json"
    save_config(sample_config, path)
    assert json.loads(path.read_text(encoding="utf-8")) == sample_config


def test_save_config_rejects_invalid_without_writing(tmp_path, sample_config):
    path = tmp_path / "out.json"
    sample_config["sync_jobs"][0]["type"] = "bogus"
    with pytest.raises(ConfigError):
        save_config(sample_config, path)
    assert not path.exists()


def test_save_config_leaves_no_temp_files(tmp_path, sample_config):
    path = tmp_path / "out.json"
    save_config(sample_config, path)
    assert [entry.name for entry in tmp_path.iterdir()] == ["out.json"]


# -- selectors -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Docs", (None, None, "docs")),
        ("rclone:Docs", ("rclone", None, "docs")),
        ("robocopy:Docs", ("robocopy", None, "docs")),
        ("rclone:NAS:Docs", ("rclone", "nas", "docs")),
        ("  RCLONE : NAS : Docs ", ("rclone", "nas", "docs")),
    ],
)
def test_parse_selector(text, expected):
    assert parse_selector(text) == expected


@pytest.mark.parametrize(
    "text",
    ["rsync:Docs", "rclone:", "a:b:c:d", "robocopy:NAS:Docs", "rclone::Docs"],
)
def test_parse_selector_rejects_nonsense(text):
    with pytest.raises(ConfigError):
        parse_selector(text)


def test_job_selector_is_unambiguous(sample_config):
    selectors = [job_selector(job) for job in sample_config["sync_jobs"]]
    assert selectors == [
        "robocopy:Docs",
        "rclone:NAS:Docs",
        "rclone:Local:Docs",
        "robocopy:Archive",
    ]


def test_no_selection_returns_enabled_jobs_in_order(sample_config):
    selected = select_jobs(sample_config)
    assert [job_selector(job) for job in selected] == [
        "robocopy:Docs",
        "rclone:NAS:Docs",
        "rclone:Local:Docs",
    ]


def test_typed_selector_picks_one_job(sample_config):
    selected = select_jobs(sample_config, ["robocopy:Docs"])
    assert [job_selector(job) for job in selected] == ["robocopy:Docs"]


def test_server_scoped_selector_disambiguates(sample_config):
    selected = select_jobs(sample_config, ["rclone:Local:Docs"])
    assert selected[0]["destination"] == "E:/Mirror/Docs"


def test_ambiguous_name_lists_the_alternatives(sample_config):
    with pytest.raises(ConfigError, match="Ambiguous job selector 'Docs'"):
        select_jobs(sample_config, ["Docs"])


def test_disabled_job_gives_a_targeted_error(sample_config):
    with pytest.raises(ConfigError, match="only matches disabled job"):
        select_jobs(sample_config, ["Archive"])


def test_unknown_job_is_reported(sample_config):
    with pytest.raises(ConfigError, match="Unknown job selector: Nope"):
        select_jobs(sample_config, ["Nope"])


def test_selection_preserves_config_order_and_deduplicates(sample_config):
    selected = select_jobs(
        sample_config, ["rclone:NAS:Docs", "robocopy:Docs", "rclone:NAS:Docs"]
    )
    assert [job_selector(job) for job in selected] == [
        "robocopy:Docs",
        "rclone:NAS:Docs",
    ]
