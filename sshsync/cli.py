"""Command-line interface: argument parsing and the live terminal display."""

import argparse
import sys
import threading
import time

from rich.console import Console
from rich.live import Live

from .config import load_config, select_jobs
from .runner import SyncRunner
from .stats import summarize
from .ui import build_history_tables, build_progress_table

# Upper bound on how often the live table is re-rendered, shared across all job
# lanes. Rendering costs ~10ms, so unbounded refreshes waste real CPU.
MIN_REFRESH_INTERVAL_SECONDS = 0.1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


def parse_args(argv=None):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="sync",
        description=(
            "Run sync jobs from config.json. Rclone jobs sync to a remote host "
            "over SFTP; robocopy jobs mirror local folders."
        ),
    )
    parser.add_argument(
        "--job",
        dest="jobs",
        action="append",
        metavar="SELECTOR",
        help=(
            "Run only selected jobs. Use NAME if unique, TYPE:NAME for "
            "type-specific selection, or TYPE:SERVER:NAME for rclone jobs that "
            "share a name across servers. Repeat to select multiple jobs."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what each backend would do without making changes.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show accumulated historical stats and exit without running jobs.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip the 'Press Enter to exit' prompt when running interactively.",
    )
    return parser.parse_args(argv)


def _is_tty(stream_name):
    stream = getattr(sys, stream_name, None)
    return bool(stream is not None and getattr(stream, "isatty", lambda: False)())


class LiveDisplay:
    """Throttled Rich live table driven by `SyncRunner` events."""

    def __init__(self, runner, console):
        self.runner = runner
        self.console = console
        self.live = None
        self._lock = threading.Lock()
        self._last_refresh_at = 0.0

    def __enter__(self):
        self.live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self.live.start()
        return self

    def __exit__(self, *exc_info):
        if self.live is not None:
            self.refresh(force=True)
            self.live.stop()
            self.live = None

    def _render(self):
        return build_progress_table(
            self.runner.job_states, total_elapsed=self.runner.elapsed_text
        )

    def refresh(self, force=False):
        """Redraw the table, skipping updates that arrive too close together."""
        if self.live is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_refresh_at < MIN_REFRESH_INTERVAL_SECONDS:
                return
            self._last_refresh_at = now
        self.live.update(self._render())

    def handle_event(self, event):
        """Consume one runner event."""
        event_type = event["type"]
        if event_type == "job_updated":
            # Status changes are worth an immediate redraw; byte counters are not.
            force = event["job"]["status"] != "RUNNING"
            self.refresh(force=force)
        elif event_type in ("run_started", "run_finished", "cancelled"):
            self.refresh(force=True)


def print_stats(console=None):
    """Print the historical stats tables."""
    console = console or Console()
    summary = summarize()

    if not summary["totals"]["runs"]:
        console.print("No saved stats yet.")
        return

    for table in build_history_tables(summary):
        console.print(table)
    console.print(f"Stats file: {summary['stats_path']}")


def _report_result(console, result):
    """Print post-run warnings, failure log locations and the outcome summary."""
    for server_name in sorted(result.offline_servers):
        console.print(
            f"[yellow][WARN][/yellow] Server '{server_name}' is offline. "
            "Skipped assigned jobs."
        )

    for log_path in sorted(result.log_paths):
        console.print(f"[cyan][INFO][/cyan] Failure details written to {log_path}")


def main(argv=None):
    """Entry point. Returns a process exit code."""
    args = parse_args(argv)
    console = Console()

    if args.stats:
        print_stats(console)
        return EXIT_OK

    config = load_config()
    selected_jobs = select_jobs(config, args.jobs)
    if not selected_jobs:
        raise RuntimeError(
            "No enabled sync jobs found. Set 'enabled': true on at least one job "
            "in config.json."
        )

    runner = SyncRunner(config, selected_jobs, dry_run=args.dry_run)

    if _is_tty("stdout"):
        # Force terminal mode so colours survive being launched from a shortcut.
        display = LiveDisplay(runner, Console(force_terminal=True, color_system="auto"))
        runner.set_listener(display.handle_event)
        with display:
            result = _run_or_cancel(runner)
    else:
        result = _run_or_cancel(runner)

    _report_result(console, result)

    if not result.ok:
        raise RuntimeError(result.summary())

    if not args.no_wait and _is_tty("stdin") and _is_tty("stdout"):
        input("Press Enter to exit...")
    return EXIT_OK


def _run_or_cancel(runner):
    """Run the batch, terminating live backends if the user interrupts."""
    try:
        return runner.run()
    except KeyboardInterrupt:
        runner.cancel()
        raise


def run():
    """Console entry point with top-level error handling."""
    console = Console(stderr=True)
    try:
        return main()
    except KeyboardInterrupt:
        console.print("[yellow][WARN][/yellow] Interrupted.")
        return EXIT_INTERRUPTED
    except Exception as exc:
        console.print(f"[red][ERROR][/red] {exc}")
        return EXIT_ERROR
