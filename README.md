# SSH-Sync

Multi-backend file synchronization for Windows, with a live-progress CLI and a
web interface for managing configuration, triggering jobs and watching them run.

One config file describes every sync job. Local mirrors go through **robocopy**;
remote transfers go over SFTP through **rclone**, authenticated with SSH keys.
Jobs that touch unrelated trees run in parallel; jobs that feed each other are
ordered automatically.

![The SSH-Sync dashboard](docs/screenshot.avif)

*The dashboard, with example servers and jobs: one NAS reachable and a laptop
that is not, each job showing its paths and how long ago it last ran. The
interface follows your system theme and can be switched from the header.*

```
python sync.py                    # run every enabled job with a live table
python sync.py --list             # every job, with the selector that names it
python sync.py --job Photos       # run one job
python sync.py --dry-run          # show what would happen, change nothing
python sync.py --stats            # historical totals
python serve.py                   # web interface on http://127.0.0.1:8420
```

## Why it exists

Backing up several trees to a NAS is easy to script badly. The problems that
shaped this tool:

- **Different jobs need different tools.** Local disk-to-disk mirroring is much
  faster with robocopy's multithreaded copy engine; anything crossing the
  network needs rclone over SFTP. One config, two backends.
- **Serial runs waste time; parallel runs corrupt data.** Uploading two trees to
  two different machines should overlap. But a job that mirrors `C:/Docs` into
  `D:/Backup` must finish before the job that uploads `D:/Backup` starts, or the
  upload captures a half-written tree. The scheduler works out which is which.
- **A NAS that is switched off should not fail the run.** Servers are probed
  over SSH first; jobs assigned to an unreachable one are skipped, and everything
  else still runs.
- **"It failed" is not a diagnosis.** Failures get their command line and the
  last 400 lines of backend output written to a dated log.

## Features

| | |
|---|---|
| **Two backends** | robocopy for local mirrors, rclone over SFTP for remotes |
| **Parallel lanes** | one lane per rclone server, one shared lane for local copies |
| **Dependency ordering** | jobs writing a tree another job reads keep their config order |
| **Offline detection** | unreachable servers are probed up front and their jobs skipped |
| **Live progress** | files, bytes, deletions and errors parsed from backend output in real time |
| **Run history** | append-only JSONL log with per-job and per-server roll-ups |
| **Failure logs** | command line plus output tail for every failed job |
| **Cancellation** | Ctrl+C (or the web Cancel button) terminates live backends across all lanes |
| **Web interface** | edit config, trigger runs, watch progress over a WebSocket, browse history |

## Requirements

- Windows (robocopy jobs) — rclone-only setups work anywhere
- Python 3.10+
- [rclone](https://rclone.org/downloads/) on `PATH`, or `rclone.exe` beside the scripts
- OpenSSH client on `PATH` (`ssh`), plus a key that authenticates to each server

## Setup

```bash
pip install -r requirements.txt
copy config.example.json config.json
```

Then edit `config.json` (or use the web interface). `config.json` is
gitignored — it holds real hostnames, usernames and key paths.

## Configuration

```jsonc
{
  "servers": [
    {
      "name": "NAS",                        // referenced by rclone jobs
      "host": "192.168.1.10",
      "user": "backup",
      "ssh_key_path": "~/.ssh/id_ed25519",  // env vars and ~ are expanded
      "port": 22
    }
  ],
  "sync_jobs": [
    {
      "name": "Documents",
      "type": "robocopy",                   // local mirror
      "source": "C:/Users/me/Documents",
      "destination": "D:/Backup/Documents"
    },
    {
      "name": "Documents",
      "type": "rclone",                     // upload over SFTP
      "server": "NAS",
      "source": "D:/Backup/Documents",      // ...the tree the job above writes
      "destination": "/srv/backup/Documents"
    }
  ]
}
```

### Job fields

| Field | Applies to | Meaning |
|---|---|---|
| `name` | all | Display name. May repeat across types and servers. |
| `type` | all | `robocopy` or `rclone`. |
| `server` | rclone | Name of an entry in `servers`. |
| `source` / `destination` | all | Paths; `~` and environment variables are expanded. |
| `enabled` | all | Defaults to `true`. Disabled jobs are skipped unless selected explicitly. |
| `blacklisted_dirnames` | all | Directory names excluded at any depth. |
| `blacklisted_filenames` | all | File name globs excluded at any depth, e.g. `*.pdb`. |
| `robocopy_mirror` | robocopy | `true` (default) deletes destination extras; `false` only adds. |
| `robocopy_level` | robocopy | Limit recursion depth. |

`node_modules`, `.vs`, `.sync`, `*.db-wal` and `*.db-shm` are excluded from every
job on both backends.

### Selecting jobs

Names may repeat, so selectors narrow them down. `--list` prints every job with
the selector that identifies it, when it last ran and how often it has failed:

```bash
python sync.py --list
```

Pass one to `--job` to run it:

```bash
python sync.py --job Photos                  # unique name
python sync.py --job robocopy:Documents      # TYPE:NAME
python sync.py --job rclone:NAS:Documents    # TYPE:SERVER:NAME
python sync.py --job Photos --job Work       # repeat to select several
```

An ambiguous selector fails and lists the valid alternatives rather than
guessing which tree to overwrite.

### Using another config

Both entry points take `--config PATH` to read a config other than the default,
which is otherwise set by `SSH_SYNC_CONFIG` or found beside the scripts:

```bash
python sync.py --config staging.json --list
python serve.py --config staging.json
```

## Web interface

```bash
python serve.py                # http://127.0.0.1:8420
python serve.py --port 9000
```

- **Dashboard** — server cards with live reachability badges, each with buttons
  to re-probe that one server or edit it. Below them a card per job showing its
  paths and how long ago it last ran, filtered by device: a chip per machine
  (plus `local` for robocopy mirrors) narrows the list to one target's jobs and
  retargets the run-all button at it. The choice is remembered per browser. Each job card has a round play button that
  runs just that job (it becomes a stop button while the run is in flight) and a
  pencil that opens an editor modal. A finished job shows its result for three
  seconds, then settles back to "last run just now". Below them, the live
  progress table and event feed.
- **History** — lifetime totals, per-job roll-ups and the most recent runs.
- **Logs** — the failure log for any day that has one, newest first.

Configuration is edited in place: the pencil on any job or server card opens a
modal, and saving validates the whole config before replacing the file
atomically, confirming with a toast. Jobs are also created and deleted there.
**Preview command** in that modal shows the exact backend command line the job
would run — built from the fields as they stand, so unsaved edits and the
dashboard's dry-run checkbox are both reflected. Useful for checking excludes
before letting a mirror delete anything.
Renaming a server rewrites every job that references it, and deleting one is
refused while jobs still point at it.

Progress streams over a WebSocket. Opening the page mid-run replays a snapshot,
so a browser that connects late still sees the whole picture.

The interface starts in whichever theme the operating system asks for and
follows it as it changes. The sun/moon button in the header overrides that; the
choice is remembered per browser.

> **Security:** the interface has no authentication, reads a config containing
> SSH key paths, and triggers local processes. It binds to `127.0.0.1` by
> default. Only expose it further on a network you control.

## Development

```bash
pip install -r requirements-dev.txt
pytest                 # 204 tests, no network or backends required
ruff check .
ruff format .
```

The suite stubs out every subprocess, so it runs on any machine without rclone,
robocopy or an SSH server. See [ARCHITECTURE.md](ARCHITECTURE.md) for the module
layout and design decisions.

### Styles

The interface is styled with [Tailwind CSS](https://tailwindcss.com) and uses
[Lucide](https://lucide.dev) icons (both embedded, nothing loaded at runtime).
The compiled stylesheet is committed, so **running the app needs Python only**.
Node is required just to change styles:

```bash
npm install
npm run build:css      # sshsync/web/styles/input.css -> static/tailwind.css
npm run watch:css      # rebuild while editing
```

Rebuild after editing markup or class names in `index.html` / `app.js`, since
Tailwind only emits the classes it finds there.

## Data locations

| What | Where |
|---|---|
| Configuration | `config.json` beside the scripts (override with `--config` or `SSH_SYNC_CONFIG`) |
| Run history | `sync-stats.jsonl`, one JSON object per completed job |
| Failure logs | `%LOCALAPPDATA%\SSH-Sync\logs\sync-YYYY-MM-DD.log` |

Logs deliberately live outside the synced trees: a file that changes on every
run would be re-uploaded every run, and could be held open while a backend
tries to replace it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All selected jobs succeeded (skipped offline jobs do not count as failures) |
| 1 | At least one job failed, or the configuration was invalid |
| 130 | Interrupted with Ctrl+C |

Robocopy's exit codes are a bitmask where values below 8 report work done rather
than errors, so only 8 and above are treated as failures.
