"""Loading, validation and persistence of config.json."""

import json
import os
import tempfile

from .paths import CONFIG_PATH

JOB_TYPES = ("rclone", "robocopy")
REQUIRED_SERVER_FIELDS = ("host", "user", "ssh_key_path")


class ConfigError(RuntimeError):
    """Raised when config.json is missing, malformed or internally inconsistent."""


def load_config(path=None):
    """Read config.json and validate its structure.

    Validation happens here rather than at use time so a typo surfaces before
    any backend runs, instead of halfway through a multi-job sync.
    """
    config_path = path or CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"Missing config file: {config_path}")

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Could not load {config_path}: {exc}") from exc

    validate_config(config)
    return config


def save_config(config, path=None):
    """Validate and atomically write config.json.

    The write goes to a temp file in the same directory and is then renamed, so
    an interrupted save can never leave a truncated config behind.
    """
    validate_config(config)
    config_path = path or CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(config, indent=4, ensure_ascii=False) + "\n"
    handle, temp_path = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temp_path, config_path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise

    return config_path


def validate_config(config):
    """Check a config object, raising ConfigError on the first problem found."""
    if not isinstance(config, dict):
        raise ConfigError("config.json must be a JSON object.")

    servers = config.get("servers")
    if not isinstance(servers, list):
        raise ConfigError("config.json section 'servers' must be an array.")

    jobs = config.get("sync_jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ConfigError("config.json section 'sync_jobs' must be a non-empty array.")

    server_names = _validate_servers(servers)
    for index, job in enumerate(jobs):
        _validate_job(index, job, server_names)

    return config


def _validate_servers(servers):
    """Validate the servers array and return the set of defined server names."""
    server_names = set()
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            raise ConfigError(f"servers[{index}] must be an object.")

        name = server.get("name")
        if not name:
            raise ConfigError(f"servers[{index}] must have a 'name' field.")
        if name in server_names:
            raise ConfigError(f"Duplicate server name: '{name}'.")
        server_names.add(name)

        for field in REQUIRED_SERVER_FIELDS:
            if not str(server.get(field, "")).strip():
                raise ConfigError(f"Server '{name}' missing required field '{field}'.")

    return server_names


def _validate_job(index, job, server_names):
    """Validate a single sync job entry."""
    if not isinstance(job, dict):
        raise ConfigError(f"sync_jobs[{index}] must be an object.")

    if not isinstance(job.get("enabled", True), bool):
        raise ConfigError(
            f"sync_jobs[{index}] field 'enabled' must be true or false when provided."
        )

    job_type = job.get("type")
    if job_type not in JOB_TYPES:
        raise ConfigError(
            f"sync_jobs[{index}] has invalid type '{job_type}'. "
            f"Use one of: {', '.join(JOB_TYPES)}."
        )

    for field in ("source", "destination"):
        if not str(job.get(field, "")).strip():
            raise ConfigError(f"sync_jobs[{index}] must have a non-empty '{field}'.")

    if job_type != "rclone":
        return

    server_ref = job.get("server")
    if not server_ref:
        raise ConfigError(f"sync_jobs[{index}] (rclone job) must have a 'server' field.")
    if server_ref not in server_names:
        raise ConfigError(f"sync_jobs[{index}] references unknown server '{server_ref}'.")


def is_enabled(job):
    """Report whether a job runs by default (jobs are enabled unless opted out)."""
    return bool(job.get("enabled", True))


def job_selector(job):
    """Build the unambiguous selector string that always identifies one job."""
    name = str(job.get("name", "default"))
    job_type = str(job.get("type", "")).lower()
    if job_type == "rclone":
        return f"rclone:{str(job.get('server', '')).strip()}:{name}"
    return f"{job_type}:{name}"


def parse_selector(request):
    """Parse `NAME`, `TYPE:NAME` or `TYPE:SERVER:NAME` into match criteria.

    Returns `(job_type, server, name)` where a `None` field matches anything.
    """
    parts = [part.strip() for part in str(request).split(":")]

    if len(parts) == 1:
        name = parts[0].lower()
        if not name:
            raise ConfigError("Empty job selector.")
        return None, None, name

    if len(parts) > 3:
        raise ConfigError(
            f"Invalid job selector '{request}'. Use NAME, TYPE:NAME or TYPE:SERVER:NAME."
        )

    job_type = parts[0].lower()
    if job_type not in JOB_TYPES:
        raise ConfigError(
            f"Invalid job selector '{request}'. "
            f"TYPE must be one of: {', '.join(JOB_TYPES)}."
        )

    name = parts[-1].lower()
    if not name:
        raise ConfigError(f"Invalid job selector '{request}'. Missing job name.")

    if len(parts) == 2:
        return job_type, None, name

    server = parts[1].lower()
    if not server:
        raise ConfigError(f"Invalid job selector '{request}'. Missing server name.")
    if job_type != "rclone":
        raise ConfigError(
            f"Invalid job selector '{request}'. "
            "TYPE:SERVER:NAME is only valid for rclone jobs."
        )
    return job_type, server, name


def _job_matches(job, criteria):
    """Report whether a job satisfies parsed selector criteria."""
    job_type, server, name = criteria
    if str(job.get("name", "default")).lower() != name:
        return False
    if job_type is not None and str(job.get("type", "")).lower() != job_type:
        return False
    return server is None or str(job.get("server", "")).lower() == server


def select_jobs(config, requested=None):
    """Resolve `--job` selectors to job objects, preserving config order.

    With no selectors, every enabled job is returned. A selector that matches
    only disabled jobs, nothing at all, or more than one job raises rather than
    silently guessing which tree to overwrite.
    """
    jobs = config["sync_jobs"]

    if not requested:
        return [job for job in jobs if is_enabled(job)]

    selected_indexes = set()
    for raw_request in requested:
        request = str(raw_request).strip()
        if not request:
            continue

        criteria = parse_selector(request)
        matches = [index for index, job in enumerate(jobs) if _job_matches(job, criteria)]
        enabled_matches = [index for index in matches if is_enabled(jobs[index])]

        if not enabled_matches:
            if matches:
                raise ConfigError(
                    f"Job selector '{request}' only matches disabled job(s). "
                    "Enable them in config.json first."
                )
            raise ConfigError(f"Unknown job selector: {request}")

        if len(enabled_matches) > 1:
            options = ", ".join(
                sorted({job_selector(jobs[index]) for index in enabled_matches})
            )
            raise ConfigError(
                f"Ambiguous job selector '{request}'. Use one of: {options}"
            )

        selected_indexes.add(enabled_matches[0])

    if not selected_indexes:
        raise ConfigError("No jobs selected from the provided --job filters.")

    return [jobs[index] for index in sorted(selected_indexes)]
