"""Bridges the thread-based `SyncRunner` to the async web layer.

`SyncRunner` blocks and emits events from worker threads, while FastAPI serves
requests on an event loop. `RunManager` owns that boundary: it runs one sync at
a time in a background thread, keeps the latest state so a browser opening
mid-run sees the whole picture, and hands events to subscribers on the loop.
"""

import asyncio
import threading
from collections import deque
from datetime import datetime, timezone

from ..config import load_config, select_jobs
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
        """Deliver an event to every subscriber, from any thread."""
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

    def _public_state(self):
        state = dict(self._state)
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

    # -- control -----------------------------------------------------------

    def start(self, selectors=None, dry_run=False):
        """Start a run in the background.

        Raises RuntimeError if one is already in progress: two concurrent runs
        would race over the same destination trees.
        """
        config = load_config()
        jobs = select_jobs(config, selectors)
        if not jobs:
            raise RuntimeError("No enabled jobs matched the requested selection.")

        with self._lock:
            if self._state["running"]:
                raise RuntimeError("A sync run is already in progress.")

            runner = SyncRunner(config, jobs, dry_run=dry_run, on_event=self._publish)
            self._runner = runner
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

        thread.start()
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

        with self._lock:
            self._state.update(
                running=False,
                finished_at=_timestamp(),
                result=result.to_dict() if result is not None else None,
                error=error,
            )

        if error is not None:
            # run_finished never fired, so tell clients the run is over anyway.
            self._publish({"type": "run_failed", "error": error})

    def cancel(self):
        """Stop the active run, if any."""
        with self._lock:
            runner = self._runner if self._state["running"] else None
        if runner is None:
            raise RuntimeError("No sync run is in progress.")
        runner.cancel()
        return self.state
