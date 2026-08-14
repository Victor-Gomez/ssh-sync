"""Command builders and connectivity checks for the sync backends."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .paths import PROJECT_ROOT
from .utils import expand_path, normalize_remote_path, require_job_value

# Robocopy copies with a single thread unless told otherwise. On large trees the
# scan dominates, and 16 threads cut the Projects job's listing pass from 6.9s to
# 0.8s. Beyond 16 there was no further gain. Incompatible with /IPG and /EFSRAW,
# neither of which this tool uses.
ROBOCOPY_THREADS = 16

# Volatile paths excluded from every job on both backends. Keep these as plain
# names/globs: each builder renders them into its own syntax (rclone --exclude
# patterns, robocopy /XD and /XF), so the two cannot drift out of sync.
GLOBAL_EXCLUDED_DIRNAMES = (".vs", ".sync", "node_modules")
GLOBAL_EXCLUDED_FILENAMES = ("*.db-wal", "*.db-shm")

# The remote name written into every generated rclone config.
RCLONE_REMOTE = "syncremote"

# Loopback aliases, tried in order when a server is configured as "localhost",
# so a host that only listens on one address family still resolves.
LOOPBACK_ALIASES = ("localhost", "127.0.0.1", "::1")


def no_window_flags():
    """Return creation flags that keep child consoles hidden on Windows."""
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def merge_excludes(job):
    """Combine the global exclude lists with a job's own blacklists."""
    dirnames = list(GLOBAL_EXCLUDED_DIRNAMES)
    for dirname in job.get("blacklisted_dirnames") or []:
        name = str(dirname)
        if name and name not in dirnames:
            dirnames.append(name)

    filenames = list(GLOBAL_EXCLUDED_FILENAMES)
    for filename in job.get("blacklisted_filenames") or []:
        name = str(filename)
        if name and name not in filenames:
            filenames.append(name)

    return dirnames, filenames


def build_filters(job):
    """Render a job's excludes as rclone `--exclude` argument pairs."""
    dirnames, filenames = merge_excludes(job)

    patterns = []
    for dirname in dirnames:
        patterns.extend([f"{dirname}/**", f"**/{dirname}/**"])
    for filename in filenames:
        patterns.extend([filename, f"**/{filename}"])

    return [part for pattern in patterns for part in ("--exclude", pattern)]


def _resolve_executable(*names, fallback=None):
    """Find a backend executable on PATH, optionally falling back to a bundled copy."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if fallback is not None and Path(fallback).is_file():
        return str(fallback)
    return None


def _ssh_candidate_hosts(host):
    """List hosts to try for an SSH check, preserving the configured value first."""
    host_value = str(host or "").strip()
    if not host_value:
        return []
    if host_value.lower() != "localhost":
        return [host_value]
    return list(LOOPBACK_ALIASES)


def check_server_ssh_access(server, timeout_seconds=4):
    """Probe a server over SSH, returning `(reachable, reason)`.

    Run before any job starts so an unplugged NAS skips its jobs cleanly rather
    than failing each one slowly through rclone's own retries.
    """
    server_name = str(server.get("name", "<unknown>"))
    host = str(server.get("host", "")).strip()
    user = str(server.get("user", "")).strip()
    key_file = expand_path(str(server.get("ssh_key_path", ""))).strip()
    port = str(server.get("port", 22)).strip()

    if not host:
        return False, f"Server '{server_name}' has empty host."
    if not user:
        return False, f"Server '{server_name}' has empty user."
    if not key_file:
        return False, f"Server '{server_name}' has empty ssh_key_path."
    if not Path(key_file).is_file():
        return False, f"Server '{server_name}' key file not found: {key_file}"

    ssh_exe = _resolve_executable("ssh", "ssh.exe")
    if not ssh_exe:
        return False, "OpenSSH client (ssh) not found on PATH."

    last_error = "SSH failed"
    for candidate_host in _ssh_candidate_hosts(host):
        command = [
            ssh_exe,
            f"-i{key_file}",
            f"-p{port}",
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oStrictHostKeyChecking=accept-new",
            f"{user}@{candidate_host}",
            "exit",
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                creationflags=no_window_flags(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = "SSH check timed out"
            continue
        except OSError as exc:
            return False, f"Server '{server_name}' SSH check failed: {exc}"

        if completed.returncode == 0:
            return True, ""

        details = (completed.stderr or completed.stdout or "SSH failed").strip()
        last_error = " ".join(details.split())

    return False, f"Server '{server_name}' is not reachable via SSH: {last_error}"


def create_temp_rclone_config(server):
    """Write a throwaway rclone config for one server and return its path.

    Credentials are never persisted to the repo: the caller deletes this file
    once the run finishes.
    """
    host = str(server.get("host", ""))
    user = str(server.get("user", ""))
    key_file = expand_path(str(server.get("ssh_key_path", "")))
    port = str(server.get("port", 22))

    if not host or not user or not key_file:
        raise RuntimeError("Server config missing host, user, or ssh_key_path.")

    content = "\n".join(
        [
            f"[{RCLONE_REMOTE}]",
            "type = sftp",
            f"host = {host}",
            f"user = {user}",
            f"key_file = {key_file}",
            f"port = {port}",
            "set_modtime = true",
            "",
        ]
    )

    handle, path = tempfile.mkstemp(prefix="rclone-sync-", suffix=".conf")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise

    return path


def build_rclone_command(job, rclone_config_path, dry_run=False):
    """Build the rclone sync command for a job."""
    rclone_exe = _resolve_executable(
        "rclone", "rclone.exe", fallback=PROJECT_ROOT / "rclone.exe"
    )
    if not rclone_exe:
        raise RuntimeError("rclone was not found on PATH. Install rclone first.")

    source_dir = expand_path(require_job_value(job, "source", "rclone"))
    destination = normalize_remote_path(require_job_value(job, "destination", "rclone"))

    command = [
        rclone_exe,
        f"--config={rclone_config_path}",
        "sync",
        source_dir,
        f"{RCLONE_REMOTE}:{destination}",
        "--delete-during",
        # No --fast-list: the SFTP backend does not implement ListR, so rclone
        # ignores the flag entirely.
        "--create-empty-src-dirs",
        "--transfers=8",
        "--checkers=16",
        "--log-level=NOTICE",
        "--stats=1s",
        "--stats-log-level=NOTICE",
        # Windows OpenSSH rejects setstat on directories with SSH_FX_FAILURE,
        # which fails the whole job. Directory mtimes are not used for syncing.
        "--no-update-dir-modtime",
        # Tolerate timestamp precision differences between local and remote.
        "--modify-window=2s",
    ]

    if dry_run:
        command.append("--dry-run")

    command.extend(build_filters(job))
    return command


def build_robocopy_command(job, dry_run=False):
    """Build the robocopy sync command for a job."""
    robocopy_exe = _resolve_executable("robocopy")
    if not robocopy_exe:
        raise RuntimeError("robocopy was not found on PATH.")

    source = expand_path(require_job_value(job, "source", "robocopy"))
    destination = expand_path(require_job_value(job, "destination", "robocopy"))

    command = [robocopy_exe, source, destination]

    # /MIR deletes destination files missing from the source; /E only adds.
    command.append("/MIR" if job.get("robocopy_mirror", True) else "/E")

    level = job.get("robocopy_level")
    if level is not None:
        command.append(f"/LEV:{int(level)}")

    command.extend(
        [
            f"/MT:{ROBOCOPY_THREADS}",
            "/r:3",  # Retry a failed file 3 times...
            "/w:3",  # ...waiting 3 seconds between attempts.
            "/NFL",  # Suppress per-file and per-directory listings so the
            "/NDL",  # output stays small enough to parse line by line.
            "/nc",
            "/ns",
            "/np",
        ]
    )

    dirnames, filenames = merge_excludes(job)
    command.extend(["/XD"] + dirnames)
    command.extend(["/XF"] + filenames)

    if dry_run:
        command.append("/L")

    return command


def build_job_command(job, rclone_config_path=None, dry_run=False):
    """Build the command for any job, returning `(command, parser_mode)`."""
    job_type = str(job.get("type", "")).lower()

    if job_type == "robocopy":
        return build_robocopy_command(job, dry_run=dry_run), "robocopy"

    if job_type == "rclone":
        if not rclone_config_path:
            raise RuntimeError(
                f"No rclone config available for job '{job.get('name', 'default')}'."
            )
        return build_rclone_command(job, rclone_config_path, dry_run=dry_run), "rclone"

    raise RuntimeError(f"Unsupported job type '{job_type}'.")
