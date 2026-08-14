"""Tests for backend command construction."""

import pytest

from sshsync import commands
from sshsync.commands import (
    GLOBAL_EXCLUDED_DIRNAMES,
    build_filters,
    build_job_command,
    build_rclone_command,
    build_robocopy_command,
    create_temp_rclone_config,
    merge_excludes,
)


@pytest.fixture(autouse=True)
def fake_executables(monkeypatch):
    """Pretend rclone and robocopy exist, so tests run on any machine."""
    monkeypatch.setattr(
        commands.shutil, "which", lambda name: f"C:/fake/{name}" if name else None
    )


ROBOCOPY_JOB = {
    "name": "Docs",
    "type": "robocopy",
    "source": "C:/Docs",
    "destination": "D:/Backup/Docs",
}

RCLONE_JOB = {
    "name": "Docs",
    "type": "rclone",
    "server": "NAS",
    "source": "C:/Docs",
    "destination": "D:/Backup/Docs",
}


# -- excludes --------------------------------------------------------------


def test_global_excludes_always_apply():
    dirnames, filenames = merge_excludes({})
    assert set(GLOBAL_EXCLUDED_DIRNAMES) <= set(dirnames)
    assert "*.db-wal" in filenames


def test_job_excludes_are_appended_without_duplicates():
    dirnames, _ = merge_excludes({"blacklisted_dirnames": ["node_modules", "obj"]})
    assert dirnames.count("node_modules") == 1
    assert "obj" in dirnames


def test_filters_render_both_root_and_nested_patterns():
    filters = build_filters({"blacklisted_dirnames": ["obj"]})
    pairs = list(zip(filters[::2], filters[1::2], strict=True))

    assert all(flag == "--exclude" for flag, _ in pairs)
    patterns = [pattern for _, pattern in pairs]
    assert "obj/**" in patterns
    assert "**/obj/**" in patterns


# -- rclone ----------------------------------------------------------------


def test_rclone_command_targets_the_generated_remote():
    command = build_rclone_command(RCLONE_JOB, "C:/tmp/rclone.conf")

    assert command[1] == "--config=C:/tmp/rclone.conf"
    assert command[2] == "sync"
    assert command[4] == "syncremote:/D:/Backup/Docs"
    assert "--delete-during" in command


def test_rclone_dry_run_adds_the_flag():
    assert "--dry-run" in build_rclone_command(RCLONE_JOB, "x.conf", dry_run=True)
    assert "--dry-run" not in build_rclone_command(RCLONE_JOB, "x.conf")


def test_rclone_requires_a_source():
    with pytest.raises(RuntimeError, match="Missing required key 'source'"):
        build_rclone_command({"name": "X", "destination": "/srv"}, "x.conf")


def test_rclone_missing_binary_is_reported(monkeypatch):
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="rclone was not found"):
        build_rclone_command(RCLONE_JOB, "x.conf")


# -- robocopy --------------------------------------------------------------


def test_robocopy_mirrors_by_default():
    command = build_robocopy_command(ROBOCOPY_JOB)
    assert "/MIR" in command
    assert "/E" not in command


def test_robocopy_mirror_can_be_disabled():
    command = build_robocopy_command({**ROBOCOPY_JOB, "robocopy_mirror": False})
    assert "/E" in command
    assert "/MIR" not in command


def test_robocopy_depth_limit_is_passed_through():
    command = build_robocopy_command({**ROBOCOPY_JOB, "robocopy_level": 2})
    assert "/LEV:2" in command


def test_robocopy_dry_run_uses_list_only():
    assert "/L" in build_robocopy_command(ROBOCOPY_JOB, dry_run=True)
    assert "/L" not in build_robocopy_command(ROBOCOPY_JOB)


def test_robocopy_excludes_follow_their_switches():
    command = build_robocopy_command(
        {
            **ROBOCOPY_JOB,
            "blacklisted_dirnames": ["obj"],
            "blacklisted_filenames": ["*.pdb"],
        }
    )
    directories = command[command.index("/XD") + 1 : command.index("/XF")]
    files = command[command.index("/XF") + 1 :]

    assert "obj" in directories
    assert "*.pdb" in files


# -- dispatch --------------------------------------------------------------


def test_build_job_command_picks_the_backend():
    command, mode = build_job_command(ROBOCOPY_JOB)
    assert mode == "robocopy" and "/MIR" in command

    command, mode = build_job_command(RCLONE_JOB, rclone_config_path="x.conf")
    assert mode == "rclone" and command[2] == "sync"


def test_build_job_command_needs_an_rclone_config():
    with pytest.raises(RuntimeError, match="No rclone config"):
        build_job_command(RCLONE_JOB)


def test_build_job_command_rejects_unknown_types():
    with pytest.raises(RuntimeError, match="Unsupported job type"):
        build_job_command({"name": "X", "type": "rsync"})


# -- temp config -----------------------------------------------------------


def test_temp_rclone_config_contains_the_server_details(tmp_path, monkeypatch):
    monkeypatch.setattr(commands.tempfile, "tempdir", str(tmp_path))
    path = create_temp_rclone_config(
        {
            "name": "NAS",
            "host": "192.168.1.10",
            "user": "backup",
            "ssh_key_path": str(tmp_path / "key"),
            "port": 2222,
        }
    )
    content = commands.Path(path).read_text(encoding="utf-8")
    commands.os.remove(path)

    assert "[syncremote]" in content
    assert "type = sftp" in content
    assert "host = 192.168.1.10" in content
    assert "port = 2222" in content


def test_temp_rclone_config_rejects_incomplete_servers():
    with pytest.raises(RuntimeError, match="missing host, user, or ssh_key_path"):
        create_temp_rclone_config({"name": "NAS", "host": "", "user": "x"})
