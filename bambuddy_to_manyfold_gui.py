#!/usr/bin/env python3
"""
bambuddy_to_manyfold_gui.py
Tkinter GUI wrapper for bambuddy_to_manyfold.py.

Requirements:
    pip install requests tqdm
    (tkinter is included with Python on macOS/Windows; on Linux: sudo apt install python3-tk)

Usage:
    python bambuddy_to_manyfold_gui.py
"""

import io
import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, scrolledtext, ttk

# ── Config persistence ────────────────────────────────────────────────────────

GUI_CONFIG_FILE = Path.home() / ".bambuddy_to_manyfold_gui.json"

FIELD_DEFAULTS = {
    "bambuddy_url":       os.getenv("BAMBUDDY_URL",       "http://localhost:8000"),
    "bambuddy_api_key":   os.getenv("BAMBUDDY_API_KEY",   ""),
    "manyfold_url":       os.getenv("MANYFOLD_URL",       "http://localhost:3214"),
    "manyfold_token":     os.getenv("MANYFOLD_TOKEN",     ""),
    "manyfold_library_id":os.getenv("MANYFOLD_LIBRARY_ID","1"),
    "sync_state_file":    os.getenv("SYNC_STATE_FILE",    "bambuddy_sync_state.json"),
}


def load_gui_config() -> dict:
    if GUI_CONFIG_FILE.exists():
        try:
            with open(GUI_CONFIG_FILE) as f:
                saved = json.load(f)
            return {**FIELD_DEFAULTS, **saved}
        except Exception:
            pass
    return dict(FIELD_DEFAULTS)


def save_gui_config(cfg: dict):
    try:
        with open(GUI_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ── Stdout redirect ───────────────────────────────────────────────────────────

class _QueueWriter(io.TextIOBase):
    """Forwards write() calls into a queue so the GUI thread can drain it."""

    def __init__(self, q: queue.Queue):
        self._q = q

    def write(self, text: str) -> int:
        if text:
            self._q.put(text)
        return len(text)

    def flush(self):
        pass


# ── Main window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bambuddy → Manyfold Sync")
        self.resizable(True, True)
        self.minsize(640, 560)

        self._cfg = load_gui_config()
        self._log_queue: queue.Queue = queue.Queue()
        self._running = False

        self._build_ui()
        self._load_fields()
        self._poll_log()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # ── Config frame ──────────────────────────────────────────────────────
        cfg_frame = ttk.LabelFrame(self, text="Configuration")
        cfg_frame.pack(fill="x", **pad)
        cfg_frame.columnconfigure(1, weight=1)

        fields = [
            ("Bambuddy URL",        "bambuddy_url",        False),
            ("Bambuddy API key",    "bambuddy_api_key",    True),
            ("Manyfold URL",        "manyfold_url",        False),
            ("Manyfold token",      "manyfold_token",      True),
            ("Manyfold library ID", "manyfold_library_id", False),
            ("Sync state file",     "sync_state_file",     False),
        ]

        self._vars: dict[str, tk.StringVar] = {}
        self._secret_entries: dict[str, ttk.Entry] = {}
        self._show_vars: dict[str, tk.BooleanVar] = {}

        for row, (label, key, secret) in enumerate(fields):
            ttk.Label(cfg_frame, text=label + ":").grid(
                row=row, column=0, sticky="e", padx=(8, 4), pady=3
            )
            var = tk.StringVar()
            self._vars[key] = var
            show = "*" if secret else ""
            entry = ttk.Entry(cfg_frame, textvariable=var, show=show, width=52)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 4), pady=3)

            if secret:
                self._secret_entries[key] = entry
                show_var = tk.BooleanVar(value=False)
                self._show_vars[key] = show_var
                ttk.Checkbutton(
                    cfg_frame, text="Show",
                    variable=show_var,
                    command=lambda e=entry, v=show_var: e.configure(show="" if v.get() else "*"),
                ).grid(row=row, column=2, padx=(0, 8), pady=3)
            elif key == "sync_state_file":
                ttk.Button(
                    cfg_frame, text="Browse…", width=8,
                    command=self._browse_state_file,
                ).grid(row=row, column=2, padx=(0, 8), pady=3)

        # ── Options row ───────────────────────────────────────────────────────
        opt_frame = ttk.Frame(self)
        opt_frame.pack(fill="x", padx=10, pady=(0, 4))

        self._dry_run_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame, text="Dry run (no uploads)", variable=self._dry_run_var
        ).pack(side="left")

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 6))

        self._run_btn = ttk.Button(btn_frame, text="▶  Run sync", command=self._start_sync)
        self._run_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = ttk.Button(
            btn_frame, text="⬛  Stop", command=self._request_stop, state="disabled"
        )
        self._stop_btn.pack(side="left")

        ttk.Button(btn_frame, text="Clear log", command=self._clear_log).pack(side="right")

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = ttk.Progressbar(self, mode="indeterminate")
        self._progress.pack(fill="x", padx=10, pady=(0, 4))

        # ── Log area ──────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Output")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        mono = font.Font(family="Courier", size=10)
        self._log = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word",
            font=mono, background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="white",
        )
        self._log.pack(fill="both", expand=True, padx=4, pady=4)

        # Colour tags
        self._log.tag_config("ok",   foreground="#4ec9b0")
        self._log.tag_config("warn", foreground="#ce9178")
        self._log.tag_config("err",  foreground="#f44747")
        self._log.tag_config("info", foreground="#9cdcfe")

    # ── Field helpers ─────────────────────────────────────────────────────────

    def _load_fields(self):
        for key, var in self._vars.items():
            var.set(self._cfg.get(key, ""))

    def _collect_fields(self) -> dict:
        return {key: var.get().strip() for key, var in self._vars.items()}

    def _browse_state_file(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
            title="Choose sync state file",
        )
        if path:
            self._vars["sync_state_file"].set(path)

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _append_log(self, text: str):
        self._log.configure(state="normal")
        # Pick a colour tag based on content
        if any(k in text for k in ("✅", "↑", "⏭")):
            tag = "ok"
        elif any(k in text for k in ("⚠️", "dry-run", "[dry")):
            tag = "warn"
        elif any(k in text for k in ("❌", "Error", "error", "failed", "Failed")):
            tag = "err"
        elif any(k in text for k in ("Fetching", "Syncing", "Checking", "ℹ️", "↓")):
            tag = "info"
        else:
            tag = ""
        self._log.insert("end", text, tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _poll_log(self):
        """Drain the log queue into the text widget; re-schedules itself every 50 ms."""
        try:
            while True:
                text = self._log_queue.get_nowait()
                self._append_log(text)
        except queue.Empty:
            pass
        self.after(50, self._poll_log)

    # ── Sync execution ────────────────────────────────────────────────────────

    def _start_sync(self):
        cfg = self._collect_fields()

        missing = []
        if not cfg["bambuddy_api_key"]:
            missing.append("Bambuddy API key")
        if not cfg["manyfold_token"]:
            missing.append("Manyfold token")
        if missing:
            messagebox.showerror(
                "Missing config", "Please fill in:\n• " + "\n• ".join(missing)
            )
            return

        # Persist config (never store secrets in the title bar, but do save them
        # to the user's home dir config so they don't have to re-enter each time)
        save_gui_config(cfg)
        self._cfg = cfg

        self._running = True
        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._progress.start(12)

        dry_run = self._dry_run_var.get()
        thread = threading.Thread(target=self._sync_worker, args=(cfg, dry_run), daemon=True)
        thread.start()

    def _request_stop(self):
        """Signal the worker that we want to stop (best-effort; can't kill mid-download)."""
        self._running = False
        self._append_log("\n⚠️  Stop requested — will halt after the current file.\n")

    def _sync_done(self):
        self._running = False
        self._progress.stop()
        self._run_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")

    def _sync_worker(self, cfg: dict, dry_run: bool):
        """Runs in a background thread. Patches env vars and redirects stdout."""
        # Inject config as env vars so the imported module picks them up
        os.environ["BAMBUDDY_URL"]        = cfg["bambuddy_url"]
        os.environ["BAMBUDDY_API_KEY"]    = cfg["bambuddy_api_key"]
        os.environ["MANYFOLD_URL"]        = cfg["manyfold_url"]
        os.environ["MANYFOLD_TOKEN"]      = cfg["manyfold_token"]
        os.environ["MANYFOLD_LIBRARY_ID"] = cfg["manyfold_library_id"]
        os.environ["SYNC_STATE_FILE"]     = cfg["sync_state_file"]

        # Re-import the module each run so module-level constants are refreshed
        import importlib
        import bambuddy_to_manyfold as sync_mod
        importlib.reload(sync_mod)

        writer = _QueueWriter(self._log_queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = writer  # type: ignore[assignment]

        try:
            import requests as _requests

            if dry_run:
                print("🔍 DRY RUN mode — nothing will be uploaded.\n")

            session = _requests.Session()
            sync_mod.check_connections(session)

            state = sync_mod.load_sync_state()
            existing_names = sync_mod.get_existing_manyfold_models(session)
            print(f"  ℹ️  {len(existing_names)} existing models found in Manyfold.\n")

            import time
            start = time.time()
            archives_added  = sync_mod.sync_archives(session, state, existing_names, dry_run)

            if not self._running:
                print("\n⚠️  Stopped before library sync.\n")
            else:
                library_added = sync_mod.sync_library_files(session, state, existing_names, dry_run)

                if not dry_run:
                    sync_mod.save_sync_state(state)

                elapsed = time.time() - start
                print(f"\n✅ Sync complete in {elapsed:.1f}s")
                print(f"   Archives uploaded    : {archives_added}")
                print(f"   Library files uploaded: {library_added}")
                if dry_run:
                    print("   (dry-run — no changes were made)")

        except SystemExit as e:
            if str(e) != "0":
                print(f"\n❌ Aborted (exit code {e})\n")
        except Exception as e:
            print(f"\n❌ Unexpected error: {e}\n")
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self.after(0, self._sync_done)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
