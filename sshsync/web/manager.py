"""Bridges the thread-based `SyncRunner` to the async web layer.

`SyncRunner` blocks and emits events from worker threads, while FastAPI serves
requests on an event loop. `RunManager` owns that boundary: it runs one sync at
a time in a background thread, keeps the latest state so a browser opening
mid-run sees the whole picture, and hands events to subscribers on the loop.

It also owns the run queue. Only one run happens at a time — two would race
over the same destination trees — so extra run requests are parked in an
in-memory queue and promoted automatically as each run ends. The queue lives
on the server, so it survives a browser reload or a closed tab; every client
sees the same pending list over the WebSocket.
"""

import asyncio
import threading
from collections import deque
from datetime import datetime, timezone

from ..config import ConfigError, job_selector, load_config, select_jobs
from ..runner import SyncRunner

# Events retained for clients that connect mid-run, so a late browser can
# replay recent activity instead of showing a blank screen.
EVENT_HISTORY_LIMIT = 500


def _timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RunManager:
    """Owns the single active sync run and fans its events out to WebSockets."""

    def __init__(self, loop=None):
        self._loop = loop
        self._lock = threading.Lock()
        self._runner = None
        self._thread = None
        self._subscribers = set()
        self._history = deque(maxlen=EVENT_HISTORY_LIMIT)
        # Runs waiting their turn: each is {"id", "selectors", "dry_run"}. The
        # front of the list starts the moment no run is in progress.
        self._queue = []
        self._queue_seq = 0
        # Selectors of the run happening right now, so a job already running
        # cannot also be queued behind itself.
        self._active_selectors = set()
        self._state = {
            "running": False,
            "dry_run": False,
            "started_at": None,
            "finished_at": None,
            "jobs": [],
            "result": None,
            "error": None,
        }

    def bind_loop(self, loop):
        """Record the event loop that owns the subscriber queues."""
        self._loop = loop

    # -- subscriptions -----------------------------------------------------

    def subscribe(self):
        """Register a queue receiving future events; returns `(queue, snapshot)`."""
        queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
            snapshot = {
                "type": "snapshot",
                "state": self._public_state(),
                "events": list(self._history),
            }
        return queue, snapshot

    def unsubscribe(self, queue):
        """Drop a subscriber queue."""
        with self._lock:
            self._subscribers.discard(queue)

    def _publish(self, event):
        """Deliver an event to every subscriber, from any thread.

        Must be called without `self._lock` held: it takes the lock itself, and
        the lock is not reentrant.
        """
        event = {"at": _timestamp(), **event}
        with self._lock:
            self._history.append(event)
            queues = list(self._subscribers)
            loop = self._loop

        if loop is None:
            return
        for queue in queues:
            # The runner emits from worker threads, so hop back to the loop.
            loop.call_soon_threadsafe(queue.put_nowait, event)

    # -- state -------------------------------------------------------------

    def _queue_public(self):
        """A copy of the queue safe to hand outside the lock."""
        return [dict(entry) for entry in self._queue]

    def _public_state(self):
        state = dict(self._state)
        state["queue"] = self._queue_public()
        if self._runner is not None:
            state["jobs"] = self._runner.job_states
            state["elapsed"] = self._runner.elapsed_text
        return state

    @property
    def state(self):
        """A JSON-serializable snapshot of the current or last run."""
        with self._lock:
            return self._public_state()

    @property
    def is_running(self):
        """Report whether a run is currently in progress."""
        with self._lock:
            return self._state["running"]

    # -- launching ---------------------------------------------------------

    def _build_and_register_locked(self, config, jobs, dry_run):
        """Wire up a runner for `jobs` and mark a run as started.

        Returns the not-yet-started thread; the caller starts it once the lock
        is released. Assumes `self._lock` is held and no run is in progress.
        """
        runner = SyncRunner(config, jobs, dry_run=dry_run, on_event=self._publish)
        self._runner = runner
        self._active_selectors = {job_selector(job) for job in jobs}
        self._state.update(
            running=True,
            dry_run=bool(dry_run),
            started_at=_timestamp(),
            finished_at=None,
            jobs=runner.job_states,
            result=None,
            error=None,
        )
        thread = threading.Thread(
            target=self._run, args=(runner,), name="sync-run", daemon=True
        )
        self._thread = thread
        return thread

    def _launch_next_locked(self, config):
        """Promote the front of the queue to a live run.

        Skips entries that no longer resolve to any enabled job — a job may have
        been disabled or removed while it waited. Returns the thread to start, or
        None if the queue is empty. Assumes `self._lock` is held and idle.
        """
        while self._queue:
            entry = self._queue.pop(0)
            try:
                jobs = select_jobs(config, entry["selectors"])
            except ConfigError:
                jobs = []
            if not jobs:
                continue
            return self._build_and_register_locked(config, list(jobs), entry["dry_run"])
        return None

    # -- control -----------------------------------------------------------

    def submit(self, items):
        """Run or queue a batch of run requests.

        `items` is a list of `{"selectors", "dry_run"}`. Each becomes its own
        run: the first starts immediately if nothing is in progress, the rest
        queue. A request is skipped when any of its jobs is already running or
        already queued, so no job is ever lined up twice.

        Returns the current state (including the queue).
        """
        config = load_config()
        # Resolve every request up front so a bad selector fails the call rather
        # than sitting silently in the queue.
        resolved = []
        for item in items:
            selectors = item.get("selectors") or None
            jobs = select_jobs(config, selectors)
            resolved.append((list(jobs), bool(item.get("dry_run", False))))

        thread = None
        with self._lock:
            taken = set(self._active_selectors) if self._state["running"] else set()
            for entry in self._queue:
                taken.update(entry["selectors"])

            for jobs, dry_run in resolved:
                selectors = [job_selector(job) for job in jobs]
                if any(selector in taken for selector in selectors):
                    continue  # already running or already queued
                self._queue_seq += 1
                self._queue.append(
                    {"id": self._queue_seq, "selectors": selectors, "dry_run": dry_run}
                )
                taken.update(selectors)

            if not self._state["running"]:
                thread = self._launch_next_locked(config)
            queue_snapshot = self._queue_public()

        self._publish({"type": "queue_updated", "queue": queue_snapshot})
        if thread is not None:
            thread.start()
        return self.state

    def dequeue(self, entry_id):
        """Drop one waiting run from the queue before it starts."""
        with self._lock:
            before = len(self._queue)
            self._queue = [entry for entry in self._queue if entry["id"] != entry_id]
            changed = len(self._queue) != before
            queue_snapshot = self._queue_public()

        if changed:
            self._publish({"type": "queue_updated", "queue": queue_snapshot})
        return self.state

    def _run(self, runner):
        """Body of the background run thread."""
        error = None
        result = None
        try:
            result = runner.run()
        except Exception as exc:
            error = str(exc)
            self._publish({"type": "log", "level": "error", "message": error})

        # Load the config before taking the lock: promoting the next run needs
        # it, and file IO under the lock would stall every other caller.
        try:
            config = load_config()
        except ConfigError:
            config = None

        with self._lock:
            self._state.update(
                running=False,
                finished_at=_timestamp(),
                result=result.to_dict() if result is not None else None,
                error=error,
            )
            self._active_selectors = set()
            thread = self._launch_next_locked(config) if config is not None else None
            queue_snapshot = self._queue_public() if thread is not None else None

        if error is not None:
            # run_finished never fired, so tell clients the run is over anyway.
            self._publish({"type": "run_failed", "error": error})
        if queue_snapshot is not None:
            # A queued run was promoted; its own run_started follows from the
            # runner, but the queue shrank, so refresh the pending list too.
            self._publish({"type": "queue_updated", "queue": queue_snapshot})
        if thread is not None:
            thread.start()

    def cancel(self):
        """Stop the active run and clear the queue.

        A stop is a full stop: anything still waiting is dropped rather than
        started the moment the current run ends.
        """
        with self._lock:
            runner = self._runner if self._state["running"] else None
            had_queued = bool(self._queue)
            self._queue = []
            queue_snapshot = self._queue_public()

        if had_queued:
            self._publish({"type": "queue_updated", "queue": queue_snapshot})
        if runner is None:
            raise RuntimeError("No sync run is in progress.")
        runner.cancel()
        return self.state
