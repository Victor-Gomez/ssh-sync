"""FastAPI application exposing config editing, job control and live progress.

The server is meant to be bound to localhost: config.json holds SSH key paths
and the run endpoints execute local backends, so there is no authentication
layer and none should be assumed.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..commands import build_job_command, check_server_ssh_access
from ..config import ConfigError, is_enabled, job_selector, load_config, save_config
from ..logs import log_path_for_today
from ..paths import CONFIG_PATH
from ..stats import load_entries, summarize
from .manager import RunManager

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Lines of the failure log returned to the browser. Enough to diagnose a run
# without shipping a multi-megabyte file into a <pre>.
LOG_TAIL_LIMIT = 500

# Default page size for the run history endpoint.
HISTORY_LIMIT = 200


def _job_summary(job, index):
    """Describe a configured job for the browser.

    `index` is the job's position in config.json — the handle the editor uses
    to write an edit back to the right entry.
    """
    return {
        "index": index,
        "name": str(job.get("name", "default")),
        "type": str(job.get("type", "")),
        "server": str(job.get("server", "")) if job.get("type") == "rclone" else "local",
        "source": str(job.get("source", "")),
        "destination": str(job.get("destination", "")),
        "enabled": is_enabled(job),
        "selector": job_selector(job),
        "blacklisted_dirnames": list(job.get("blacklisted_dirnames") or []),
        "blacklisted_filenames": list(job.get("blacklisted_filenames") or []),
    }


def create_app():
    """Build the FastAPI application."""
    manager = RunManager()

    @asynccontextmanager
    async def lifespan(_app):
        # Subscriber queues belong to the serving loop, so capture it on startup.
        manager.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="SSH-Sync", version=__version__, lifespan=lifespan)
    app.state.manager = manager

    # -- config ------------------------------------------------------------

    @app.get("/api/config")
    def get_config():
        """Return the raw config plus derived per-job summaries."""
        try:
            config = load_config()
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "path": str(CONFIG_PATH),
            "config": config,
            "jobs": [
                _job_summary(job, index) for index, job in enumerate(config["sync_jobs"])
            ],
            "servers": config["servers"],
        }

    @app.put("/api/config")
    def put_config(config=Body(...)):
        """Validate and persist a replacement config."""
        try:
            save_config(config)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return get_config()

    @app.post("/api/config/validate")
    def validate(config=Body(...)):
        """Check a config without saving it, so the editor can warn as you type."""
        from ..config import validate_config

        try:
            validate_config(config)
        except ConfigError as exc:
            return {"valid": False, "error": str(exc)}
        return {"valid": True, "error": None}

    @app.post("/api/jobs/preview")
    def preview_command(payload=Body(...)):
        """Show the exact backend command a job would run.

        Useful for verifying excludes and dry-run flags before touching files.
        """
        selector = str(payload.get("selector", ""))
        try:
            config = load_config()
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        job = next(
            (item for item in config["sync_jobs"] if job_selector(item) == selector),
            None,
        )
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job '{selector}'.")

        try:
            command, mode = build_job_command(
                job,
                rclone_config_path="<generated at run time>",
                dry_run=bool(payload.get("dry_run", False)),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"selector": selector, "parser": mode, "command": command}

    # -- servers -----------------------------------------------------------

    @app.post("/api/servers/check")
    def check_servers(payload=Body(default=None)):
        """Probe servers over SSH — all of them, or only those in `names`."""
        try:
            config = load_config()
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        servers = config["servers"]
        wanted = (payload or {}).get("names")
        if wanted:
            wanted = {str(name) for name in wanted}
            servers = [server for server in servers if server.get("name") in wanted]
            if not servers:
                raise HTTPException(
                    status_code=404, detail="No configured server matched that name."
                )

        results = []
        for server in servers:
            reachable, reason = check_server_ssh_access(server)
            results.append(
                {
                    "name": server.get("name"),
                    "host": server.get("host"),
                    "online": reachable,
                    "reason": reason,
                }
            )
        return {"servers": results}

    # -- runs --------------------------------------------------------------

    @app.get("/api/status")
    def status():
        """Return the state of the current or most recent run."""
        return manager.state

    @app.post("/api/run")
    def start_run(payload=Body(default=None)):
        """Trigger a run over the given selectors (all enabled jobs if omitted)."""
        payload = payload or {}
        selectors = payload.get("selectors") or None
        try:
            return manager.start(selectors, dry_run=bool(payload.get("dry_run", False)))
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/cancel")
    def cancel_run():
        """Stop the active run."""
        try:
            return manager.cancel()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -- history and logs --------------------------------------------------

    @app.get("/api/history")
    def history(limit: int = HISTORY_LIMIT):
        """Return recent run records, newest first."""
        entries = load_entries(limit=max(1, min(limit, 5000)))
        return {"entries": list(reversed(entries))}

    @app.get("/api/history/summary")
    def history_summary():
        """Return aggregated totals across the whole run history."""
        return summarize()

    @app.get("/api/logs")
    def failure_log():
        """Return the tail of today's failure log."""
        path = log_path_for_today()
        if not path.is_file():
            return {"path": str(path), "exists": False, "lines": []}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"path": str(path), "exists": True, "lines": lines[-LOG_TAIL_LIMIT:]}

    # -- live feed ---------------------------------------------------------

    @app.websocket("/ws")
    async def live_feed(websocket: WebSocket):
        """Stream run events, starting with a snapshot of current state."""
        await websocket.accept()
        queue, snapshot = manager.subscribe()
        try:
            await websocket.send_json(snapshot)
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            manager.unsubscribe(queue)

    # -- static ------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        """Serve the single-page interface.

        Sent with `no-cache` so the browser revalidates the shell on every
        load: a stale index paired with fresh /static assets renders a page
        whose markup and script disagree.
        """
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    return app
