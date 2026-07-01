#!/usr/bin/env python3
"""
bambuddy_manyfold_web.py — Flask web GUI for bambuddy_to_manyfold.

Wraps the sync engine in bambuddy_to_manyfold.py with a browser UI: a config
form (persisted to disk), a "load models" step that lists Bambuddy archives +
library files with their synced/new status, a selective sync run with live
progress streamed over Server-Sent Events, and an empty-model cleanup action.

The engine module is not modified — this file reuses the exact same
"mutate os.environ, then importlib.reload the engine module" trick the
Tkinter GUI (bambuddy_to_manyfold_gui.py) already uses to apply config before
each run. Only one load/sync/cleanup job runs at a time (enforced by
_job_run_lock), which is what makes that reload trick safe under gunicorn's
threaded workers: there's never a second thread relying on the module's
globals while a reload is in flight.

Run:
    pip install flask requests tqdm
    python bambuddy_manyfold_web.py     # then open the printed URL

Copyright (C) 2026 Victor Manuel (hibikipr)
SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sys

if sys.version_info < (3, 10):
    sys.exit(f"❌ Python 3.10+ required (this is {sys.version.split()[0]}).")

import datetime
import importlib
import io
import json
import logging
import os
import queue
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8089"))


# ── Config persistence ────────────────────────────────────────────────────────
# Same field set + env-var-as-default precedence as bambuddy_to_manyfold_gui.py's
# FIELD_DEFAULTS/load_gui_config, just relocated to a Docker-friendly path.

def _default_web_config_file() -> Path:
    env_path = os.getenv("WEB_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    return Path.home() / ".bambuddy_to_manyfold_web.json"


WEB_CONFIG_FILE = _default_web_config_file()

FIELD_DEFAULTS = {
    "bambuddy_url": os.getenv("BAMBUDDY_URL", "http://localhost:8000"),
    "bambuddy_api_key": os.getenv("BAMBUDDY_API_KEY", ""),
    "manyfold_url": os.getenv("MANYFOLD_URL", "http://localhost:3214"),
    "manyfold_client_id": os.getenv("MANYFOLD_CLIENT_ID", ""),
    "manyfold_client_secret": os.getenv("MANYFOLD_CLIENT_SECRET", ""),
    "manyfold_token": os.getenv("MANYFOLD_TOKEN", ""),
    "manyfold_library_id": os.getenv("MANYFOLD_LIBRARY_ID", "1"),
    "sync_state_file": os.getenv("SYNC_STATE_FILE", "bambuddy_sync_state.json"),
    "debug": os.getenv("MANYFOLD_SYNC_DEBUG", "").lower() in ("1", "true", "yes", "on"),
}

SECRET_FIELDS = {"bambuddy_api_key", "manyfold_client_id", "manyfold_client_secret", "manyfold_token"}
MASK = "••••••••"


def load_web_config() -> dict:
    if WEB_CONFIG_FILE.exists():
        try:
            saved = json.loads(WEB_CONFIG_FILE.read_text())
            return {**FIELD_DEFAULTS, **saved}
        except Exception:
            pass
    return dict(FIELD_DEFAULTS)


def save_web_config(cfg: dict):
    WEB_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEB_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _validate_config(cfg: dict) -> list[str]:
    """Same rule as the Tkinter GUI's _validate_config."""
    missing = []
    if not cfg.get("bambuddy_api_key"):
        missing.append("Bambuddy API key")
    has_client_creds = bool(cfg.get("manyfold_client_id") and cfg.get("manyfold_client_secret"))
    if not has_client_creds and not cfg.get("manyfold_token"):
        missing.append("Manyfold client ID + secret (or a token)")
    return missing


def _set_env(cfg: dict):
    """Push the saved config into process env vars, for the engine module to read on reload."""
    os.environ["BAMBUDDY_URL"] = cfg["bambuddy_url"]
    os.environ["BAMBUDDY_API_KEY"] = cfg["bambuddy_api_key"]
    os.environ["MANYFOLD_URL"] = cfg["manyfold_url"]
    os.environ["MANYFOLD_CLIENT_ID"] = cfg["manyfold_client_id"]
    os.environ["MANYFOLD_CLIENT_SECRET"] = cfg["manyfold_client_secret"]
    os.environ["MANYFOLD_TOKEN"] = cfg["manyfold_token"]
    os.environ["MANYFOLD_LIBRARY_ID"] = cfg["manyfold_library_id"]
    os.environ["SYNC_STATE_FILE"] = cfg["sync_state_file"]
    os.environ["MANYFOLD_SYNC_DEBUG"] = "1" if cfg.get("debug") else "0"


def _load_sync_module():
    """(Re)import the engine so its module-level config globals re-read os.environ.

    Only ever called from inside a job that's already holding _job_run_lock, so
    there's no concurrent thread relying on the previous module object.
    """
    import bambuddy_to_manyfold as sync_mod
    importlib.reload(sync_mod)
    return sync_mod


# ── Background job + SSE machinery ───────────────────────────────────────────

class SyncJob:
    """Tracks one load/sync/cleanup run: status, an append-only log buffer that
    new SSE subscribers replay from the start, and live subscriber queues."""

    def __init__(self, kind: str, options: dict | None = None):
        self.id = uuid.uuid4().hex
        self.kind = kind  # "load" | "sync" | "cleanup"
        self.options = options or {}
        self.status = "running"  # running | done | error
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.log_lines: list[str] = []
        self.subscribers: list["queue.Queue[str]"] = []
        self.result: dict | None = None
        self.cancel_requested = False
        self._lock = threading.Lock()

    def emit(self, text: str):
        if not text:
            return
        with self._lock:
            self.log_lines.append(text)
            for q in self.subscribers:
                q.put(text)

    def subscribe(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue()
        with self._lock:
            for line in self.log_lines:  # replay everything so far
                q.put(line)
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]"):
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def finish(self, status: str):
        self.status = status
        self.finished_at = time.time()


class _JobWriter(io.TextIOBase):
    """stdout/stderr redirect target during a job — forwards write() to the job's log."""

    def __init__(self, job: SyncJob):
        self._job = job

    def write(self, text: str) -> int:
        self._job.emit(text)
        return len(text)

    def flush(self):
        pass


_job_run_lock = threading.Lock()
_current_job: SyncJob | None = None
_last_models: dict | None = None  # {"archives": [...], "library_files": [...]} from the last load job


def _try_start_job(kind: str, options: dict, target) -> SyncJob | None:
    """Start a job in a background thread if none is currently running.

    Returns the new SyncJob, or None (caller should respond 409) if a job is
    already in flight — _job_run_lock is the single point of mutual exclusion
    that also makes the engine's env-var + module-reload trick safe.
    """
    global _current_job
    if not _job_run_lock.acquire(blocking=False):
        return None

    job = SyncJob(kind, options)
    _current_job = job

    def _runner():
        writer = _JobWriter(job)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = writer  # type: ignore[assignment]
        try:
            target(job)
        except SystemExit as e:
            if str(e) != "0":
                job.emit(f"\n❌ Aborted (exit code {e})\n")
                job.finish("error")
            else:
                job.finish("done")
        except Exception as e:
            job.emit(f"\n❌ Unexpected error: {e}\n")
            job.finish("error")
        else:
            if job.status == "running":
                job.finish("done")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            _job_run_lock.release()

    threading.Thread(target=_runner, daemon=True).start()
    return job


def _banner(emoji: str, label: str, dry_run: bool = False):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'─' * 60}")
    print(f"  {emoji}  {label} at {ts}" + ("  [DRY RUN]" if dry_run else ""))
    print(f"{'─' * 60}\n")


# ── Job targets — thin wrappers around the unmodified engine functions ───────

STRIP_EXTS = {".gcode", ".3mf", ".stl", ".obj", ".step", ".stp"}
SUPPORTED_LIB_EXTS = {".3mf", ".stl", ".obj", ".step", ".stp"}


def _strip_ext(name: str) -> str:
    n = name
    while Path(n).suffix.lower() in STRIP_EXTS:
        n = Path(n).stem
    return n


def _run_load(job: SyncJob):
    global _last_models
    sync_mod = _load_sync_module()
    session = requests.Session()

    _banner("⟳", "Loading models")
    sync_mod.check_connections(session)

    state = sync_mod.load_sync_state()
    synced_archive_ids = set(state.get("synced_archives", []))
    synced_file_ids = set(state.get("synced_library_files", []))

    archives = sync_mod.get_bambuddy_archives(session)
    lib_files = sync_mod.get_bambuddy_library_files(session)

    flat_folders = sync_mod._flatten_folders(sync_mod.get_bambuddy_library_folders(session))
    folder_by_id = {f["id"]: f for f in flat_folders}

    archive_rows = []
    for a in archives:
        aid = a.get("id")
        name = a.get("name") or a.get("filename", f"archive_{aid}")
        archive_rows.append({
            "id": aid,
            "name": _strip_ext(name),
            "status": "synced" if aid in synced_archive_ids else "new",
            "date": a.get("created_at") or a.get("created") or "",
        })

    supported = [
        lf for lf in lib_files
        if Path(lf.get("filename") or lf.get("name", "")).suffix.lower() in SUPPORTED_LIB_EXTS
    ]
    lib_rows = []
    for lf in supported:
        fid = lf.get("id")
        fname = lf.get("filename") or lf.get("name", "")
        stem = _strip_ext(fname)
        folder_id = lf.get("folder_id")
        folder_path = folder_by_id[folder_id]["_full_path"] if folder_id in folder_by_id else None
        lib_rows.append({
            "id": fid,
            "name": f"{folder_path}/{stem}" if folder_path else stem,
            "status": "synced" if fid in synced_file_ids else "new",
            "date": lf.get("created_at") or lf.get("created") or "",
        })

    print(f"\n  ℹ️  Loaded {len(archive_rows)} archive(s) and {len(lib_rows)} library file(s).\n")

    job.result = {"archives": archive_rows, "library_files": lib_rows}
    _last_models = job.result


def _run_sync(job: SyncJob, options: dict):
    sync_mod = _load_sync_module()
    session = requests.Session()

    dry_run = bool(options.get("dry_run"))
    selected_archive_ids = options.get("selected_archive_ids")
    selected_library_file_ids = options.get("selected_library_file_ids")
    selected_archive_ids = set(selected_archive_ids) if selected_archive_ids is not None else None
    selected_library_file_ids = (
        set(selected_library_file_ids) if selected_library_file_ids is not None else None
    )

    _banner("🚀", "Sync started", dry_run)
    sync_mod.check_connections(session)

    state = sync_mod.load_sync_state()
    existing_names = sync_mod.get_existing_manyfold_models(session)
    print(f"  ℹ️  {len(existing_names)} existing models found in Manyfold.\n")

    start = time.time()
    archives_added = sync_mod.sync_archives(
        session, state, existing_names, dry_run,
        selected_ids=selected_archive_ids,
        create_missing=options.get("create_missing", True),
        force=options.get("force", False),
    )

    library_added = 0
    if job.cancel_requested:
        print("\n⚠️  Stopped before library sync.\n")
    else:
        library_added = sync_mod.sync_library_files(
            session, state, existing_names, dry_run,
            selected_ids=selected_library_file_ids,
            create_missing=options.get("create_missing", True),
            force=options.get("force", False),
            add_source_links=options.get("add_links", True),
            enrich_from_makerworld=options.get("enrich", True),
            group_makerworld_profiles=options.get("group_makerworld", True),
        )
        if not dry_run:
            sync_mod.save_sync_state(state)

    elapsed = time.time() - start
    print(f"\n✅ Sync complete in {elapsed:.1f}s")
    print(f"   Archives uploaded     : {archives_added}")
    print(f"   Library files uploaded: {library_added}")
    if dry_run:
        print("   (dry-run — no changes were made)")

    job.result = {
        "archives_added": archives_added,
        "library_added": library_added,
        "elapsed": elapsed,
        "dry_run": dry_run,
    }


def _run_cleanup(job: SyncJob, collection: str, dry_run: bool):
    sync_mod = _load_sync_module()
    session = requests.Session()

    _banner("🧹", "Cleanup started", dry_run)

    scopes = sync_mod.MANYFOLD_SCOPES
    if "delete" not in scopes.split():
        scopes = f"{scopes} delete"
    if not sync_mod.obtain_manyfold_token(session, scopes=scopes):
        print("❌ Could not obtain a delete-capable token.")
        job.result = {"deleted": 0}
        return

    target = None if collection.upper() == "ALL" else collection
    deleted = sync_mod.cleanup_empty_models(session, target, dry_run)
    print(f"\n✅ Cleanup complete — {deleted} empty model(s) "
          f"{'would be ' if dry_run else ''}deleted.")
    job.result = {"deleted": deleted}


# ── Routes: pages + PWA assets ────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole site.
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    resp = app.send_static_file("manifest.webmanifest")
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


# ── Routes: config ────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    cfg = load_web_config()
    reveal = request.args.get("reveal") == "1"
    out = dict(cfg)
    if not reveal:
        for key in SECRET_FIELDS:
            out[f"{key}_set"] = bool(cfg.get(key))
            out[key] = MASK if cfg.get(key) else ""
    return jsonify(out)


@app.post("/api/config")
def post_config():
    body = request.get_json(force=True, silent=True) or {}
    cfg = load_web_config()
    for key in FIELD_DEFAULTS:
        if key not in body:
            continue
        if key == "debug":
            cfg[key] = bool(body[key])
            continue
        val = str(body[key]).strip()
        # A masked placeholder means the user didn't touch this field — keep
        # the value already on disk instead of overwriting it with dots.
        if key in SECRET_FIELDS and val == MASK:
            continue
        cfg[key] = val
    missing = _validate_config(cfg)
    if missing:
        return jsonify(ok=False, missing=missing), 400
    save_web_config(cfg)
    return jsonify(ok=True)


# ── Routes: health ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Reachability check. Deliberately reimplements the two HTTP calls inline
    instead of calling the engine's check_connections()/_set_env()+reload
    pattern — that mutates process-wide env vars and reloads the shared module,
    which would race with a sync/load/cleanup job's own reload if one were
    running concurrently. A read-only status probe should never touch that
    shared state."""
    cfg = load_web_config()
    missing = _validate_config(cfg)
    if missing:
        return jsonify(ok=False, error="Missing config: " + ", ".join(missing))

    bambuddy_url = cfg["bambuddy_url"].rstrip("/")
    manyfold_url = cfg["manyfold_url"].rstrip("/")
    session = requests.Session()

    try:
        r = session.get(
            f"{bambuddy_url}/api/v1/system/info",
            headers={"X-API-Key": cfg["bambuddy_api_key"], "Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        return jsonify(ok=False, error=f"Cannot reach Bambuddy: {e}")

    token = cfg.get("manyfold_token") or ""
    if cfg.get("manyfold_client_id") and cfg.get("manyfold_client_secret"):
        try:
            tr = session.post(
                f"{manyfold_url}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": cfg["manyfold_client_id"],
                    "client_secret": cfg["manyfold_client_secret"],
                    "scope": "public read write upload",
                },
                timeout=15,
            )
            if not tr.ok:
                return jsonify(ok=False, error=f"Manyfold token request failed: {tr.status_code}")
            token = tr.json().get("access_token", token)
        except Exception as e:
            return jsonify(ok=False, error=f"Cannot reach Manyfold token endpoint: {e}")

    try:
        r = session.get(
            f"{manyfold_url}/models",
            params={"page": 1},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.manyfold.v0+json"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        return jsonify(ok=False, error=f"Cannot reach Manyfold: {e}")

    return jsonify(ok=True, bambuddy_url=bambuddy_url, manyfold_url=manyfold_url)


# ── Routes: models + jobs ─────────────────────────────────────────────────────

@app.post("/api/models/load")
def start_load():
    cfg = load_web_config()
    missing = _validate_config(cfg)
    if missing:
        return jsonify(ok=False, error="Missing config: " + ", ".join(missing)), 400
    _set_env(cfg)

    job = _try_start_job("load", {}, _run_load)
    if job is None:
        return jsonify(ok=False, error="A job is already running"), 409
    return jsonify(ok=True, job_id=job.id), 202


@app.get("/api/models")
def get_models():
    return jsonify(_last_models or {"archives": [], "library_files": []})


@app.post("/api/sync/start")
def start_sync():
    cfg = load_web_config()
    missing = _validate_config(cfg)
    if missing:
        return jsonify(ok=False, error="Missing config: " + ", ".join(missing)), 400
    _set_env(cfg)

    body = request.get_json(force=True, silent=True) or {}
    options = {
        "dry_run": bool(body.get("dry_run")),
        "create_missing": bool(body.get("create_missing", True)),
        "force": bool(body.get("force")),
        "add_links": bool(body.get("add_links", True)),
        "enrich": bool(body.get("enrich", True)),
        "group_makerworld": bool(body.get("group_makerworld", True)),
        "selected_archive_ids": body.get("selected_archive_ids"),
        "selected_library_file_ids": body.get("selected_library_file_ids"),
    }

    job = _try_start_job("sync", options, lambda job: _run_sync(job, options))
    if job is None:
        return jsonify(ok=False, error="A job is already running"), 409
    return jsonify(ok=True, job_id=job.id), 202


@app.post("/api/cleanup/start")
def start_cleanup():
    cfg = load_web_config()
    missing = _validate_config(cfg)
    if missing:
        return jsonify(ok=False, error="Missing config: " + ", ".join(missing)), 400
    _set_env(cfg)

    body = request.get_json(force=True, silent=True) or {}
    collection = (body.get("collection") or "MakerWorld").strip() or "MakerWorld"
    dry_run = bool(body.get("dry_run"))

    job = _try_start_job(
        "cleanup", {"collection": collection, "dry_run": dry_run},
        lambda job: _run_cleanup(job, collection, dry_run),
    )
    if job is None:
        return jsonify(ok=False, error="A job is already running"), 409
    return jsonify(ok=True, job_id=job.id), 202


@app.post("/api/sync/cancel")
def cancel_sync():
    job = _current_job
    if job is None or job.status != "running":
        return jsonify(ok=False, error="No job is running"), 409
    job.cancel_requested = True
    job.emit("\n⚠️  Stop requested — will halt after the current phase.\n")
    return jsonify(ok=True)


@app.get("/api/sync/status")
def sync_status():
    job = _current_job
    if job is None:
        return jsonify(running=False)
    return jsonify(
        running=job.status == "running",
        job_id=job.id,
        kind=job.kind,
        status=job.status,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=job.result,
    )


@app.get("/api/sync/stream")
def sync_stream():
    job = _current_job

    def generate():
        if job is None:
            yield "event: idle\ndata: {}\n\n"
            return
        q = job.subscribe()
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    line = q.get(timeout=1)
                    yield f"data: {json.dumps({'line': line})}\n\n"
                except queue.Empty:
                    if job.status != "running":
                        break
                    if time.time() - last_heartbeat > 15:
                        yield ": keep-alive\n\n"
                        last_heartbeat = time.time()
            yield f"event: done\ndata: {json.dumps({'status': job.status, 'result': job.result})}\n\n"
        finally:
            job.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print(f"  Open on your phone or browser:  http://<this-host>:{PORT}/")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
