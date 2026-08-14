# Architecture

SSH-Sync is a small engine with two front ends. The engine plans and runs jobs
and emits events; the CLI renders those events as a Rich table, and the web app
forwards them over a WebSocket. Neither front end contains sync logic, and the
engine contains no display logic.

```
                       ┌──────────────┐        ┌──────────────┐
 sync.py  ────────────▶│  sshsync.cli │        │ sshsync.web  │◀──────  serve.py
                       └──────┬───────┘        └──────┬───────┘
                              │  events               │  events
                              ▼                       ▼
                       ┌───────────────────────────────────────┐
                       │          sshsync.runner               │
                       │  SyncRunner: lanes, ordering, results │
                       └───┬───────────┬───────────┬───────────┘
                           │           │           │
                  ┌────────▼──┐  ┌─────▼─────┐  ┌──▼──────────┐
                  │  planner  │  │ commands  │  │  executor   │
                  │  lanes &  │  │  argv for │  │ run + parse │
                  │   order   │  │ backends  │  │   output    │
                  └───────────┘  └───────────┘  └──────┬──────┘
                                                       │
                                   ┌───────────────────▼──────┐
                                   │  robocopy.exe / rclone   │
                                   └──────────────────────────┘
```

## Modules

| Module | Responsibility |
|---|---|
| `sshsync/paths.py` | Every filesystem location, resolved from the project root |
| `sshsync/config.py` | Loading, validation, atomic saving, job selectors |
| `sshsync/planner.py` | Pure scheduling: lane grouping and dependency detection |
| `sshsync/commands.py` | Backend argv construction, exclude rendering, SSH probes |
| `sshsync/executor.py` | Subprocess streaming and output parsing into stats |
| `sshsync/runner.py` | `SyncRunner` — orchestration, cancellation, events, results |
| `sshsync/stats.py` | Append-only run history and its roll-ups |
| `sshsync/logs.py` | Failure logging |
| `sshsync/utils.py` | Path expansion and display formatting |
| `sshsync/ui.py` | Rich tables |
| `sshsync/cli.py` | Argument parsing and the throttled live display |
| `sshsync/web/manager.py` | Thread-to-event-loop bridge, one active run |
| `sshsync/web/app.py` | REST endpoints and the WebSocket feed |
| `sshsync/web/static/` | Single-page interface: markup, vanilla JS, compiled CSS |
| `sshsync/web/styles/` | Tailwind source, compiled into `static/tailwind.css` |

Entry points `sync.py` and `serve.py` sit at the project root and do nothing but
delegate.

## Design decisions

### Lanes, not a thread pool

Jobs are grouped into **lanes** that run concurrently while each lane runs its
own jobs sequentially:

- one lane per rclone server, so two servers sync in parallel;
- one shared lane for all robocopy jobs, so several local mirrors do not thrash
  the same physical disks at once.

### Dependencies across lanes

Splitting jobs into lanes can break an ordering the config implied. A robocopy
job that writes `D:/Backup` and an rclone job that uploads it land in different
lanes and would otherwise overlap, capturing a half-written tree.

`planner.build_job_dependencies` compares the local paths each job reads and
writes. Any pair that overlaps keeps its original config order; everything else
is free to run concurrently. An rclone job pointed at a loopback host counts as
a local write, because that "remote" is this machine.

The comparison is pure string work on normalized paths, so the whole schedule is
computed - and tested - without touching a disk.

### Backends are parsed, not trusted

Neither backend offers a machine-readable progress format, so `executor.py`
parses their human output. `RcloneParser` and `RobocopyParser` are separate
classes with the same `feed(stats, line)` / `finalize(stats, exit_code)`
interface, holding no I/O — which is why they can be tested against captured
output rather than a live sync.

`finalize` exists for robocopy specifically: it prints `ERROR` lines for files it
then retries successfully, so those only count once the exit code agrees. Its
exit codes are a bitmask where anything below 8 reports work done.

### Events instead of a shared UI

`SyncRunner` never imports Rich or FastAPI. It calls `on_event` with plain
dicts (`run_started`, `job_updated`, `run_finished`, …), and listener exceptions
are swallowed — a broken UI must not abort a sync midway through writing files.

That indirection is what made the web interface additive rather than a rewrite:
`LiveDisplay` throttles events into table redraws, `RunManager` fans the same
events out to WebSocket subscribers.

### Crossing the thread boundary

`SyncRunner` blocks and emits from worker threads; FastAPI serves on an event
loop. `RunManager` owns that boundary. It runs one sync at a time in a
background thread and hands events to subscriber queues with
`loop.call_soon_threadsafe`. It also keeps the latest state and a bounded event
history, so a browser opening mid-run gets a snapshot rather than a blank page.

Only one run is allowed at a time: two concurrent runs would race over the same
destination trees. That constraint is visible in the UI — while a run is in
flight, every other job's play button is disabled, and the running job's button
becomes a stop control.

### Identity across the wire

Job names repeat across types and servers, so the browser matches live progress
to its cards by **selector** (`rclone:NAS:Documents`), which `job_selector`
guarantees is unique. Editing uses a different handle — the job's **index** in
`config.json` — because that is what an edit must be written back to. Both are
sent with every job payload.

### Committing the compiled CSS

Tailwind needs a build step, which sits awkwardly with a tool whose whole
install is `pip install -r requirements.txt`. The compromise: the compiled
stylesheet is committed, so running the app never touches npm — only changing
styles does. It also keeps the interface working with no network, which matters
for something that manages local backups.

Utilities carry layout, spacing and colour directly on the elements. The
`@layer components` block in `styles/input.css` holds only patterns repeated
across many elements — buttons in four variants, tags, inputs, table cells —
where inlining nine utilities per instance would bury the markup. The palette
lives in `@theme` as tokens (`bg-panel`, `text-dim`, `border-edge`), so a colour
changes in one place.

### Two themes, one set of names

Every colour in the interface is a token, so a theme is a second set of values
for the same names: `:root[data-theme="light"]` re-points them and nothing else
in the stylesheet mentions a theme. Shadows are the exception to living in
`@theme` — Tailwind resolves a themed shadow into the utility at build time,
which would freeze them at the dark values, so they stay plain custom
properties referenced as `shadow-[var(--shadow-modal)]`.

The attribute is set by a small inline script in `<head>`, from the stored
choice or `prefers-color-scheme`, before the stylesheet paints — doing it in
`app.js` would flash the wrong palette first. `app.js` only keeps the toggle in
sync, and follows the OS until the user picks a side of their own.

Elements the JavaScript has to find again are marked with `data-*` hooks
(`[data-card="job"]`, `[data-role="footer"]`) rather than styling classes, so
restyling can never break a query selector.

### Editing without a separate config view

Config edits go through modals opened from the cards themselves, and each save
sends the whole config to `PUT /api/config`, which validates it before an atomic
replace. Sending the whole document rather than a patch means the validator sees
what the file will actually contain — cross-references like a job naming a
server that no longer exists are caught server-side rather than reimplemented in
the browser.

### Append-only history

Run history is JSONL — one line appended per completed job. Recording a result
costs the same whether the file holds ten runs or ten thousand, and a truncated
final write only costs that one line. `summarize()` makes a single pass to feed
the global, per-job and per-server views at once.

### Cancellation

Live subprocesses are tracked in a set. `cancel()` sets an event and terminates
them all, so Ctrl+C in one lane stops work in the others. Lanes poll their
dependency events with a timeout rather than blocking, because an untimed lock
acquire is not interruptible by Ctrl+C on Windows.

## Adding a backend

1. `commands.py` — add `build_<backend>_command(job, dry_run=False)` and a branch
   in `build_job_command`.
2. `executor.py` — add a parser class with `feed`/`finalize` and register it in
   `PARSERS`.
3. `config.py` — add the type to `JOB_TYPES`.
4. `utils.py` — extend `is_failed_exit_code` if its exit codes are unusual.
5. `planner.py` — only if it has unusual locality (like the loopback case).

Nothing in `runner.py`, `cli.py` or the web layer needs to change.

## Testing

166 tests, no network and no backends required — every subprocess is stubbed.

| File | Covers |
|---|---|
| `test_utils.py` | Formatting, path normalization, exit-code rules |
| `test_config.py` | Validation, atomic saving, selector resolution |
| `test_planner.py` | Lane grouping, dependency detection, loopback handling |
| `test_commands.py` | argv construction, excludes, dry-run flags |
| `test_executor.py` | Output parsing against captured backend output |
| `test_stats.py` | History round-trips, corruption tolerance, roll-ups, migration |
| `test_runner.py` | Ordering, skipping, failures, cancellation, events |
| `test_web.py` | REST surface, config persistence, WebSocket handshake |
