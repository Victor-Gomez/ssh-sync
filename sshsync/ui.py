"""Rich tables for the terminal interface."""

from rich import box
from rich.table import Table

from .stats import format_aggregate_elapsed, format_ended_at
from .utils import format_bytes

STATUS_STYLES = {
    "OK": "bold green",
    "FAIL": "bold red",
    "RUNNING": "bold yellow",
    "CANCELLED": "bold red",
    "SKIPPED": "bold blue",
    "PENDING": "dim",
}

# Longest error snippet shown inline before the failure log takes over.
MAX_INLINE_ERROR_CHARS = 60


def _status_text(status):
    return f"[{STATUS_STYLES.get(status, 'dim')}]{status}[/]"


def _count_and_size(count, size_bytes):
    return f"{count} ({format_bytes(size_bytes)})"


def build_progress_table(job_states, total_elapsed=""):
    """Build the live progress table for a run."""
    table = Table(
        box=box.ROUNDED,
        header_style="bold",
        row_styles=["none", "dim"],
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Job", style="bold white", min_width=16)
    table.add_column("Server", justify="center", min_width=12)
    table.add_column("Status", justify="center", min_width=10)
    table.add_column("Uploaded", justify="right", style="bright_green")
    table.add_column("Deleted", justify="right", style="bright_red")
    table.add_column("Files", justify="right", style="cyan")
    table.add_column("Elapsed", justify="right", style="magenta")
    table.add_column("Errors", justify="left", style="yellow")

    totals = dict.fromkeys(
        (
            "uploaded_files",
            "uploaded_bytes",
            "deleted_files",
            "deleted_bytes",
            "checks_done",
            "checks_total",
            "source_bytes",
        ),
        0,
    )

    for state in job_states:
        stats = state["stats"]
        for field in (
            "uploaded_files",
            "uploaded_bytes",
            "deleted_files",
            "deleted_bytes",
            "checks_done",
            "checks_total",
        ):
            totals[field] += stats[field]

        # source_bytes stays None until the background sizing pass reaches this job.
        source_bytes = state.get("source_bytes")
        totals["source_bytes"] += source_bytes or 0
        size_text = "..." if source_bytes is None else format_bytes(source_bytes)

        errors_text = str(stats["error_count"])
        if stats["last_error"]:
            errors_text += f" ({stats['last_error'][:MAX_INLINE_ERROR_CHARS]})"

        table.add_row(
            state["name"],
            state.get("server", ""),
            _status_text(state["status"]),
            _count_and_size(stats["uploaded_files"], stats["uploaded_bytes"]),
            _count_and_size(stats["deleted_files"], stats["deleted_bytes"]),
            f"{stats['checks_done']}/{stats['checks_total']} ({size_text})",
            stats["elapsed"],
            errors_text,
        )

    table.add_row(
        "TOTAL",
        "",
        "",
        _count_and_size(totals["uploaded_files"], totals["uploaded_bytes"]),
        _count_and_size(totals["deleted_files"], totals["deleted_bytes"]),
        f"{totals['checks_done']}/{totals['checks_total']} "
        f"({format_bytes(totals['source_bytes'])})",
        total_elapsed,
        "",
        style="bold white on rgb(40,40,40)",
    )
    return table


def build_history_tables(summary):
    """Build the summary, per-job and per-server tables for `--stats`."""
    totals = summary["totals"]

    overall = Table(title="SSH-Sync Historical Totals")
    overall.add_column("Metric", style="bold")
    overall.add_column("Value", justify="right")
    overall.add_row("Total runs", str(totals["runs"]))
    overall.add_row("Successful runs", str(totals["runs"] - totals["failures"]))
    overall.add_row("Failed runs", str(totals["failures"]))
    overall.add_row(
        "Uploaded", _count_and_size(totals["uploaded_files"], totals["uploaded_bytes"])
    )
    overall.add_row(
        "Deleted", _count_and_size(totals["deleted_files"], totals["deleted_bytes"])
    )
    overall.add_row(
        "Files", _count_and_size(totals["files_total"], totals["files_total_bytes"])
    )
    overall.add_row("Errors", str(totals["error_count"]))

    shared_columns = (
        ("Runs", "right"),
        ("Failures", "right"),
        ("Last Run", "left"),
        ("Total Elapsed", "right"),
        ("Uploaded", "right"),
        ("Deleted", "right"),
        ("Files", "right"),
        ("Errors", "right"),
    )

    def shared_cells(item):
        return [
            str(item["runs"]),
            str(item["failures"]),
            format_ended_at(item["last_ended_at"]),
            format_aggregate_elapsed(item),
            _count_and_size(item["uploaded_files"], item["uploaded_bytes"]),
            _count_and_size(item["deleted_files"], item["deleted_bytes"]),
            _count_and_size(item["files_total"], item["files_total_bytes"]),
            str(item["error_count"]),
        ]

    jobs = Table(title="Per-Job Historical Totals")
    for column in ("Job", "Type", "Server"):
        jobs.add_column(column)
    for title, justify in shared_columns:
        jobs.add_column(title, justify=justify)
    for item in summary["jobs"]:
        jobs.add_row(item["name"], item["type"], item["server"], *shared_cells(item))

    servers = Table(title="Per-Server Historical Totals")
    servers.add_column("Server")
    for title, justify in shared_columns:
        servers.add_column(title, justify=justify)
    for item in summary["servers"]:
        servers.add_row(item["server"], *shared_cells(item))

    return overall, jobs, servers
