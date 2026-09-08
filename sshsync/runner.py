"""The sync engine: plans, executes and reports on a set of jobs.

`SyncRunner` owns everything about running a batch of jobs and knows nothing
about how progress is displayed. It emits plain-dict events instead, which the
CLI renders as a Rich table and the web app forwards over a WebSocket.
"""

import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures

from .commands import (
    build_job_command,
    check_server_ssh_access,
    compute_rclone_concurrency,
    create_temp_rclone_config,
    measure_latency_ms,
)
from .config import job_selector
from .executor import new_stats, stream_command
from .logs import LOG_TAIL_LINES, write_failure_log
from .planner import build_job_dependencies, build_job_lanes, required_rclone_servers
from .stats import append_job_stats
from .utils import (
    expand_path,
    format_elapsed,
    get_directory_size_bytes,
    is_failed_exit_code,
    profile_directory,
)

# Source trees usually live on different physical drives, so measuring a few at
# once is faster than walking them one after another.
MAX_SIZING_WORKERS = 4

# Cap the pre-sync profiling walk used to tune rclone concurrency. Past this many
# files the tuning is already saturated (see compute_rclone_concurrency), so the
# rest of the walk would only delay the job it is meant to speed up.
CONCURRENCY_FILE_CAP = 25_000

# How often a lane checks whether the job it depends on has finished. Polling
# beats a blocking wait because an untimed lock acquire is not interruptible by
# Ctrl+C on Windows.
DEPENDENCY_POLL_SECONDS = 0.25

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_OK = "OK"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"
STATUS_CANCELLED = "CANCELLED"


def initial_job_state(index, job):
    """Build the starting display state for one job.

    `index` is the position within this run, not within the config; `selector`
    is the stable identity, which is what the web UI matches its cards against.
    """
    job_type = str(job.get("type", ""))
    return {
        "index": index,
        "selector": job_selector(job),
        "name": str(job.get("name", "default")),
        "type": job_type,
        "server": (
            str(job.get("server", "")) if job_type.lower() == "rclone" else "local"
        ),
        "source": str(job.get("source", "")),
        "destination": str(job.get("destination", "")),
        # Filled in by the background sizing pass; None renders as "...".
        "source_bytes": None,
        "status": STATUS_PENDING,
        "stats": new_stats(),
    }


class RunResult:
    """Outcome of a completed run."""

    def __init__(self, job_states, failures, lane_errors, offline_servers, log_paths):
        self.job_states = job_states
        self.failures = failures
        self.lane_errors = lane_errors
        self.offline_servers = offline_servers
        self.log_paths = log_paths

    @property
    def ok(self):
        """Report whether every job that ran succeeded."""
        return not self.failures and not self.lane_errors

    def summary(self):
        """Build a one-line human summary of the run."""
        if self.lane_errors:
            details = "; ".join(str(error) for error in self.lane_errors)
            return f"{len(self.lane_errors)} sync lane(s) errored: {details}"
        if self.failures:
            details = ", ".join(f"{name} (exit {code})" for name, code in self.failures)
            return f"{len(self.failures)} sync job(s) failed: {details}"
        return f"{len(self.job_states)} job(s) completed successfully."

    def to_dict(self):
        """Render the result as JSON-serializable data."""
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "jobs": self.job_states,
            "failures": [
                {"name": name, "exit_code": code} for name, code in self.failures
            ],
            "lane_errors": [str(error) for error in self.lane_errors],
            "offline_servers": self.offline_servers,
            "log_paths": sorted(self.log_paths),
        }


class SyncRunner:
    """Run a selection of sync jobs with lane parallelism and live events."""

    def __init__(self, config, jobs, dry_run=False, on_event=None):
        self.config = config
        self.jobs = list(jobs)
        self.dry_run = bool(dry_run)
        self._on_event = on_event

        self.servers_by_name = {
            server.get("name"): server for server in config.get("servers", [])
        }
        self.job_states = [
            initial_job_state(index, job) for index, job in enumerate(self.jobs)
        ]

        self.started_at = None
        self._cancel_event = threading.Event()
        self._active_processes = set()
        self._process_lock = threading.Lock()
        self._failures = []
        self._log_paths = set()
        self._failures_lock = threading.Lock()
        # Latency is a per-server property, so probe each server once and reuse
        # the reading across all of its jobs.
        self._latency_ms = {}
        self._latency_lock = threading.Lock()

    # -- events ------------------------------------------------------------

    def set_listener(self, callback):
        """Attach (or clear, with None) the callback receiving run events."""
        self._on_event = callback

    def emit(self, event_type, **payload):
        """Send an event to the listener, if any.

        Listener failures are swallowed: a broken UI must not abort a sync that
        is midway through writing files.
        """
        if self._on_event is None:
            return
        try:
            self._on_event({"type": event_type, **payload})
        except Exception:
            pass

    def _emit_job(self, index):
        self.emit("job_updated", index=index, job=self.job_states[index])

    @property
    def elapsed(self):
        """Seconds since the run started, or 0 before it does."""
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    @property
    def elapsed_text(self):
        """Formatted total run time."""
        return format_elapsed(self.elapsed)

    # -- cancellation ------------------------------------------------------

    def cancel(self):
        """Stop the run and terminate any backend processes still executing."""
        self._cancel_event.set()
        with self._process_lock:
            processes = list(self._active_processes)
        for process in processes:
            self._terminate(process)
        self.emit("cancelled")

    @property
    def cancelled(self):
        """Report whether cancellation has been requested."""
        return self._cancel_event.is_set()

    @staticmethod
    def _terminate(process):
        try:
            process.terminate()
        except OSError:
            pass

    def _track_process(self, process, running):
        with self._process_lock:
            if not running:
                self._active_processes.discard(process)
                return
            self._active_processes.add(process)
            cancelled = self._cancel_event.is_set()
        if cancelled:
            # Cancellation raced the process start; stop it immediately.
            self._terminate(process)

    # -- run ---------------------------------------------------------------

    def run(self):
        """Execute every selected job and return a `RunResult`."""
        self.started_at = time.monotonic()
        self.emit("run_started", jobs=self.job_states, dry_run=self.dry_run)

        offline_servers = self._check_servers()
        runnable = self._mark_offline_jobs(offline_servers)

        if not runnable:
            return self._finish(offline_servers)

        rclone_configs = self._create_rclone_configs(runnable)
        size_executor, size_futures = self._start_source_sizing()

        lanes = build_job_lanes(self.jobs)
        dependencies = build_job_dependencies(self.jobs, lanes, self.servers_by_name)
        done_events = [threading.Event() for _ in self.jobs]
        lane_errors = []

        lane_executor = ThreadPoolExecutor(
            max_workers=max(1, len(lanes)), thread_name_prefix="lane"
        )
        try:
            pending = {
                lane_executor.submit(
                    self._run_lane,
                    indexes,
                    dependencies,
                    done_events,
                    rclone_configs,
                    size_futures,
                    set(runnable),
                )
                for indexes in lanes.values()
            }
            # Poll rather than block indefinitely so Ctrl+C is delivered
            # promptly on Windows.
            while pending:
                done, pending = wait_for_futures(pending, timeout=DEPENDENCY_POLL_SECONDS)
                for future in done:
                    error = future.exception()
                    if error is not None:
                        lane_errors.append(error)
        except BaseException:
            self.cancel()
            raise
        finally:
            lane_executor.shutdown(wait=True)
            size_executor.shutdown(wait=False, cancel_futures=True)
            self._cleanup_rclone_configs(rclone_configs)

        return self._finish(offline_servers, lane_errors)

    def _finish(self, offline_servers, lane_errors=None):
        result = RunResult(
            self.job_states,
            list(self._failures),
            list(lane_errors or []),
            offline_servers,
            set(self._log_paths),
        )
        self.emit("run_finished", result=result.to_dict(), elapsed=self.elapsed_text)
        return result

    # -- setup phases ------------------------------------------------------

    def _check_servers(self):
        """Probe every server the selected rclone jobs need, in parallel."""
        server_names = required_rclone_servers(self.jobs)
        for name in server_names:
            if name not in self.servers_by_name:
                raise RuntimeError(f"Unknown server '{name}' referenced by rclone jobs.")

        if not server_names:
            return {}

        self.emit("checking_servers", servers=server_names)

        offline = {}
        with ThreadPoolExecutor(
            max_workers=len(server_names), thread_name_prefix="ssh-check"
        ) as pool:
            futures = {
                name: pool.submit(self._probe_server, self.servers_by_name[name])
                for name in server_names
            }
            for name, future in futures.items():
                reachable, reason, latency = future.result()
                if reachable:
                    # Cache latency now, alongside the reachability check, so no
                    # job pays a probe on its own critical path.
                    with self._latency_lock:
                        self._latency_ms[name] = latency
                else:
                    offline[name] = reason
                    self.emit(
                        "log",
                        level="warning",
                        message=f"Server '{name}' is offline: {reason}",
                    )

        return offline

    @staticmethod
    def _probe_server(server):
        """Check reachability and, if up, measure latency in one pooled task.

        Returns `(reachable, reason, latency_ms)`; latency is `None` when the
        server is offline (nothing to probe) or the reading fails.
        """
        reachable, reason = check_server_ssh_access(server)
        latency = measure_latency_ms(server) if reachable else None
        return reachable, reason, latency

    def _mark_offline_jobs(self, offline_servers):
        """Mark jobs on unreachable servers as skipped; return the runnable ones."""
        runnable = []
        for index, job in enumerate(self.jobs):
            is_offline = (
                str(job.get("type", "")).lower() == "rclone"
                and str(job.get("server", "")) in offline_servers
            )
            if is_offline:
                state = self.job_states[index]
                state["status"] = STATUS_SKIPPED
                state["stats"]["last_error"] = offline_servers[str(job.get("server"))]
                self._emit_job(index)
            else:
                runnable.append(index)
        return runnable

    def _create_rclone_configs(self, runnable_indexes):
        """Write one temporary rclone config per server actually being used."""
        configs = {}
        for index in runnable_indexes:
            job = self.jobs[index]
            if str(job.get("type", "")).lower() != "rclone":
                continue
            server_name = job.get("server")
            if server_name in configs:
                continue
            server = self.servers_by_name.get(server_name)
            if not server:
                raise RuntimeError(
                    f"Job '{job.get('name')}' references unknown server '{server_name}'."
                )
            configs[server_name] = create_temp_rclone_config(server)
        return configs

    @staticmethod
    def _cleanup_rclone_configs(configs):
        """Delete the generated rclone configs; they hold key paths."""
        for path in configs.values():
            try:
                os.remove(path)
            except OSError:
                pass

    def _start_source_sizing(self):
        """Measure source tree sizes in the background.

        The figure is only needed for the Files column and the stats record, so
        jobs must not wait on the walk.
        """
        executor = ThreadPoolExecutor(
            max_workers=MAX_SIZING_WORKERS, thread_name_prefix="sizer"
        )
        futures = {}

        def on_ready(source, future):
            try:
                value = int(future.result())
            except Exception:
                value = 0
            for state in self.job_states:
                if state["source"] == source:
                    state["source_bytes"] = value
                    self._emit_job(state["index"])

        for state in self.job_states:
            source = state["source"]
            if source in futures:
                continue
            future = executor.submit(
                lambda path=source: get_directory_size_bytes(expand_path(path))
            )
            future.add_done_callback(lambda done, source=source: on_ready(source, done))
            futures[source] = future

        return executor, futures

    @staticmethod
    def _resolve_source_bytes(futures, source):
        """Read a finished sizing result, defaulting to 0 on any failure."""
        future = futures.get(source)
        if future is None:
            return 0
        try:
            return int(future.result())
        except Exception:
            return 0

    # -- execution ---------------------------------------------------------

    def _run_lane(
        self, indexes, dependencies, done_events, rclone_configs, size_futures, runnable
    ):
        """Run one lane's jobs sequentially, honouring cross-lane dependencies."""
        try:
            for index in indexes:
                if index not in runnable:
                    continue
                if self.cancelled or not self._wait_for_dependencies(
                    dependencies[index], done_events
                ):
                    return
                try:
                    self._run_job(index, rclone_configs, size_futures)
                finally:
                    done_events[index].set()
        finally:
            # Never leave another lane waiting on a job this lane will not reach.
            for index in indexes:
                done_events[index].set()

    def _wait_for_dependencies(self, dependency_indexes, done_events):
        """Block until prerequisites finish; return False if cancelled first."""
        for index in dependency_indexes:
            while not done_events[index].wait(DEPENDENCY_POLL_SECONDS):
                if self.cancelled:
                    return False
        return True

    def _server_latency_ms(self, server_name):
        """Round-trip time to a server, probed once and cached for the run."""
        with self._latency_lock:
            if server_name in self._latency_ms:
                return self._latency_ms[server_name]

        server = self.servers_by_name.get(server_name)
        latency = measure_latency_ms(server) if server else None

        with self._latency_lock:
            self._latency_ms[server_name] = latency
        return latency

    def _rclone_concurrency(self, job):
        """Compute `(transfers, checkers)` for an rclone job at run time.

        Returns `(None, None)` for non-rclone jobs so the builder keeps its own
        defaults. A job may pin explicit `transfers`/`checkers` in config, which
        skips profiling entirely.
        """
        if str(job.get("type", "")).lower() != "rclone":
            return None, None
        if job.get("transfers") or job.get("checkers"):
            return job.get("transfers"), job.get("checkers")

        source = expand_path(str(job.get("source", "")))
        file_count, total_bytes = profile_directory(
            source, file_cap=CONCURRENCY_FILE_CAP
        )
        latency = self._server_latency_ms(job.get("server"))
        transfers, checkers = compute_rclone_concurrency(
            file_count, total_bytes, latency
        )

        capped = "+" if file_count >= CONCURRENCY_FILE_CAP else ""
        latency_text = f"{latency:.0f}ms" if latency is not None else "unknown"
        self.emit(
            "log",
            level="info",
            message=(
                f"Job '{job.get('name')}' tuned rclone: {file_count}{capped} files, "
                f"{latency_text} latency -> --transfers={transfers} "
                f"--checkers={checkers}"
            ),
        )
        return transfers, checkers

    def _run_job(self, index, rclone_configs, size_futures):
        """Execute a single job and record its outcome."""
        job = self.jobs[index]
        state = self.job_states[index]
        job_name = state["name"]

        state["status"] = STATUS_RUNNING
        self._emit_job(index)

        transfers, checkers = self._rclone_concurrency(job)
        command, parser_mode = build_job_command(
            job,
            rclone_config_path=rclone_configs.get(job.get("server")),
            dry_run=self.dry_run,
            transfers=transfers,
            checkers=checkers,
        )

        def on_update(current_stats):
            state["stats"] = current_stats
            self._emit_job(index)

        tail_lines = deque(maxlen=LOG_TAIL_LINES)
        exit_code, stats = stream_command(
            command,
            on_update=on_update,
            parser_mode=parser_mode,
            on_process=self._track_process,
            line_buffer=tail_lines,
        )
        state["stats"] = stats

        if self.cancelled:
            state["status"] = STATUS_CANCELLED
            self._emit_job(index)
            return

        if is_failed_exit_code(parser_mode, exit_code):
            state["status"] = STATUS_FAIL
            log_path = write_failure_log(
                job_name,
                state["server"],
                command,
                exit_code,
                stats,
                list(tail_lines),
            )
            with self._failures_lock:
                self._failures.append((job_name, exit_code))
                if log_path is not None:
                    self._log_paths.add(str(log_path))
        else:
            state["status"] = STATUS_OK

        append_job_stats(
            job=job,
            stats=stats,
            status=state["status"],
            exit_code=exit_code,
            source_bytes=self._resolve_source_bytes(size_futures, state["source"]),
            dry_run=self.dry_run,
        )

        self._emit_job(index)
