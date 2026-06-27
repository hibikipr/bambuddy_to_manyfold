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
    "bambuddy_url":           os.getenv("BAMBUDDY_URL",           "http://localhost:8000"),
    "bambuddy_api_key":       os.getenv("BAMBUDDY_API_KEY",       ""),
    "manyfold_url":           os.getenv("MANYFOLD_URL",           "http://localhost:3214"),
    "manyfold_client_id":     os.getenv("MANYFOLD_CLIENT_ID",     ""),
    "manyfold_client_secret": os.getenv("MANYFOLD_CLIENT_SECRET", ""),
    "manyfold_token":         os.getenv("MANYFOLD_TOKEN",         ""),
    "manyfold_library_id":    os.getenv("MANYFOLD_LIBRARY_ID",    "1"),
    "sync_state_file":        os.getenv("SYNC_STATE_FILE",        "bambuddy_sync_state.json"),
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

    def __init__(self, q: "queue.Queue[str]"):
        self._q = q

    def write(self, text: str) -> int:
        if text:
            self._q.put(text)
        return len(text)

    def flush(self):
        pass


# ── Checkbox Treeview helper ──────────────────────────────────────────────────

CHECK_ON  = "✓"
CHECK_OFF = "☐"


class CheckTree:
    """
    A ttk.Treeview with virtual per-row checkboxes.

    Columns: "check" (30 px), "name" (stretch), "status" (70 px).
    Clicking any cell on a row toggles its checkbox.
    """

    def __init__(self, parent: tk.Widget):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)

        self._tree = ttk.Treeview(
            frame,
            columns=("check", "name", "status"),
            show="headings",
            selectmode="none",
        )
        self._tree.heading("check",  text="")
        self._tree.heading("name",   text="Name")
        self._tree.heading("status", text="Status")
        self._tree.column("check",  width=30,  stretch=False, anchor="center")
        self._tree.column("name",   stretch=True,             anchor="w")
        self._tree.column("status", width=70,  stretch=False, anchor="center")

        sb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Tag for already-synced rows
        self._tree.tag_configure("synced", foreground="#888888")

        # Toggle on any click
        self._tree.bind("<Button-1>", self._on_click)

        # iid → bool (checked state)
        self._checks: dict[str, bool] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def clear(self):
        self._tree.delete(*self._tree.get_children())
        self._checks.clear()

    def add_row(self, iid: str, name: str, status: str, checked: bool = True):
        self._checks[iid] = checked
        tags = ("synced",) if status == "synced" else ()
        self._tree.insert(
            "", "end", iid=iid,
            values=(CHECK_ON if checked else CHECK_OFF, name, status),
            tags=tags,
        )

    def set_all(self, checked: bool):
        for iid in self._tree.get_children():
            self._checks[iid] = checked
            self._tree.set(iid, "check", CHECK_ON if checked else CHECK_OFF)

    def checked_iids(self) -> list[str]:
        return [iid for iid, v in self._checks.items() if v]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_click(self, event: tk.Event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        new_state = not self._checks.get(iid, True)
        self._checks[iid] = new_state
        self._tree.set(iid, "check", CHECK_ON if new_state else CHECK_OFF)


# ── Main window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bambuddy → Manyfold Sync")
        self.resizable(True, True)
        self.minsize(720, 640)

        self._cfg = load_gui_config()
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._running = False

        # Raw data loaded by "Load models"
        self._archives: list[dict] = []
        self._lib_files: list[dict] = []

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
            ("Bambuddy URL",          "bambuddy_url",           False),
            ("Bambuddy API key",      "bambuddy_api_key",       True),
            ("Manyfold URL",          "manyfold_url",           False),
            ("Manyfold client ID",    "manyfold_client_id",     True),
            ("Manyfold client secret","manyfold_client_secret", True),
            ("Manyfold token (alt)",  "manyfold_token",         True),
            ("Manyfold library ID",   "manyfold_library_id",    False),
            ("Sync state file",       "sync_state_file",        False),
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

        # ── Options + top buttons ─────────────────────────────────────────────
        top_btn_frame = ttk.Frame(self)
        top_btn_frame.pack(fill="x", padx=10, pady=(0, 4))

        self._dry_run_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top_btn_frame, text="Dry run (no uploads)", variable=self._dry_run_var
        ).pack(side="left", padx=(0, 12))

        self._create_missing_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top_btn_frame, text="Create missing models in Manyfold", variable=self._create_missing_var
        ).pack(side="left", padx=(0, 12))

        self._force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top_btn_frame, text="Force re-sync (ignore sync state)", variable=self._force_var
        ).pack(side="left", padx=(0, 12))

        self._debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top_btn_frame, text="Log debug", variable=self._debug_var
        ).pack(side="left", padx=(0, 12))

        self._load_btn = ttk.Button(
            top_btn_frame, text="⟳  Load models", command=self._start_load
        )
        self._load_btn.pack(side="left", padx=(0, 6))

        self._run_btn = ttk.Button(
            top_btn_frame, text="▶  Run sync", command=self._start_sync, state="disabled"
        )
        self._run_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = ttk.Button(
            top_btn_frame, text="⬛  Stop", command=self._request_stop, state="disabled"
        )
        self._stop_btn.pack(side="left")

        ttk.Button(
            top_btn_frame, text="Clear log", command=self._clear_log
        ).pack(side="right")

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = ttk.Progressbar(self, mode="indeterminate")
        self._progress.pack(fill="x", padx=10, pady=(0, 4))

        # ── Vertical paned window: selection on top, log on bottom ────────────
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ── Model selection pane ──────────────────────────────────────────────
        sel_outer = ttk.LabelFrame(paned, text="Model selection  —  click a row to toggle")
        paned.add(sel_outer, weight=2)

        # Archives sub-section
        arch_hdr = ttk.Frame(sel_outer)
        arch_hdr.pack(fill="x", padx=4, pady=(4, 0))
        self._arch_label = ttk.Label(arch_hdr, text="Archives  (load models first)")
        self._arch_label.pack(side="left")
        ttk.Button(arch_hdr, text="All",  width=4,
                   command=lambda: self._arch_tree.set_all(True)).pack(side="right", padx=(2, 0))
        ttk.Button(arch_hdr, text="None", width=4,
                   command=lambda: self._arch_tree.set_all(False)).pack(side="right")

        self._arch_tree = CheckTree(sel_outer)

        ttk.Separator(sel_outer, orient="horizontal").pack(fill="x", padx=4, pady=4)

        # Library files sub-section
        lib_hdr = ttk.Frame(sel_outer)
        lib_hdr.pack(fill="x", padx=4, pady=(0, 0))
        self._lib_label = ttk.Label(lib_hdr, text="Library files  (load models first)")
        self._lib_label.pack(side="left")
        ttk.Button(lib_hdr, text="All",  width=4,
                   command=lambda: self._lib_tree.set_all(True)).pack(side="right", padx=(2, 0))
        ttk.Button(lib_hdr, text="None", width=4,
                   command=lambda: self._lib_tree.set_all(False)).pack(side="right")

        self._lib_tree = CheckTree(sel_outer)

        # ── Log pane ──────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(paned, text="Output")
        paned.add(log_frame, weight=1)

        mono = font.Font(family="Courier", size=10)
        self._log = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word",
            font=mono, background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="white",
        )
        self._log.pack(fill="both", expand=True, padx=4, pady=4)

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
        try:
            while True:
                self._append_log(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(50, self._poll_log)

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> dict | None:
        cfg = self._collect_fields()
        missing = []
        if not cfg["bambuddy_api_key"]:
            missing.append("Bambuddy API key")
        # Need EITHER client ID + secret OR a pre-issued token.
        has_client_creds = bool(cfg["manyfold_client_id"] and cfg["manyfold_client_secret"])
        if not has_client_creds and not cfg["manyfold_token"]:
            missing.append("Manyfold client ID + secret (or a token)")
        if missing:
            messagebox.showerror(
                "Missing config", "Please fill in:\n• " + "\n• ".join(missing)
            )
            return None
        save_gui_config(cfg)
        self._cfg = cfg
        return cfg

    def _set_env(self, cfg: dict):
        os.environ["BAMBUDDY_URL"]            = cfg["bambuddy_url"]
        os.environ["BAMBUDDY_API_KEY"]        = cfg["bambuddy_api_key"]
        os.environ["MANYFOLD_URL"]            = cfg["manyfold_url"]
        os.environ["MANYFOLD_CLIENT_ID"]      = cfg["manyfold_client_id"]
        os.environ["MANYFOLD_CLIENT_SECRET"]  = cfg["manyfold_client_secret"]
        os.environ["MANYFOLD_TOKEN"]          = cfg["manyfold_token"]
        os.environ["MANYFOLD_LIBRARY_ID"]     = cfg["manyfold_library_id"]
        os.environ["SYNC_STATE_FILE"]         = cfg["sync_state_file"]
        os.environ["MANYFOLD_SYNC_DEBUG"]     = "1" if self._debug_var.get() else "0"

    # ── Load models ───────────────────────────────────────────────────────────

    def _start_load(self):
        cfg = self._validate_config()
        if cfg is None:
            return
        self._running = True
        self._load_btn.configure(state="disabled")
        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._progress.start(12)
        threading.Thread(target=self._load_worker, args=(cfg,), daemon=True).start()

    def _load_worker(self, cfg: dict):
        self._set_env(cfg)

        import importlib
        import bambuddy_to_manyfold as sync_mod
        importlib.reload(sync_mod)

        writer = _QueueWriter(self._log_queue)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = writer  # type: ignore[assignment]

        archives: list[dict] = []
        lib_files: list[dict] = []
        synced_archive_ids: set = set()
        synced_file_ids: set = set()

        try:
            import datetime
            import requests as _requests
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{'─' * 60}")
            print(f"  ⟳  Loading models at {ts}")
            print(f"{'─' * 60}\n")

            session = _requests.Session()
            sync_mod.check_connections(session)

            # Load sync state to mark already-synced items
            state = sync_mod.load_sync_state()
            synced_archive_ids = set(state.get("synced_archives", []))
            synced_file_ids    = set(state.get("synced_library_files", []))

            archives  = sync_mod.get_bambuddy_archives(session)
            lib_files = sync_mod.get_bambuddy_library_files(session)

            # Enrich library files with folder path for display
            flat_folders = sync_mod._flatten_folders(sync_mod.get_bambuddy_library_folders(session))
            folder_by_id = {f["id"]: f for f in flat_folders}
            STRIP_EXTS = {".gcode", ".3mf", ".stl", ".obj", ".step", ".stp"}
            for lf in lib_files:
                fname = lf.get("filename") or lf.get("name", "")
                stem = fname
                while Path(stem).suffix.lower() in STRIP_EXTS:
                    stem = Path(stem).stem
                folder_id = lf.get("folder_id")
                folder_path = folder_by_id[folder_id]["_full_path"] if folder_id in folder_by_id else None
                lf["_display_name"] = f"{folder_path}/{stem}" if folder_path else stem

            print(f"\n  ℹ️  Loaded {len(archives)} archive(s) and {len(lib_files)} library file(s).\n")

        except SystemExit as e:
            if str(e) != "0":
                print(f"\n❌ Aborted (exit code {e})\n")
        except Exception as e:
            print(f"\n❌ Error loading models: {e}\n")
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
            self.after(
                0,
                lambda: self._load_done(
                    archives, lib_files, synced_archive_ids, synced_file_ids
                ),
            )

    def _load_done(
        self,
        archives: list[dict],
        lib_files: list[dict],
        synced_archive_ids: set,
        synced_file_ids: set,
    ):
        self._running = False
        self._progress.stop()
        self._load_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")

        self._archives  = archives
        self._lib_files = lib_files

        STRIP_EXTS = {".gcode", ".3mf", ".stl", ".obj", ".step", ".stp"}

        # Populate archives tree
        self._arch_tree.clear()
        for a in archives:
            aid   = a.get("id")
            name  = a.get("name") or a.get("filename", f"archive_{aid}")
            stem  = name
            while Path(stem).suffix.lower() in STRIP_EXTS:
                stem = Path(stem).stem
            status  = "synced" if aid in synced_archive_ids else "new"
            checked = status != "synced"
            self._arch_tree.add_row(str(aid), stem, status, checked)

        self._arch_label.configure(
            text=f"Archives  ({len(archives)} total, "
                 f"{sum(1 for a in archives if a.get('id') in synced_archive_ids)} already synced)"
        )

        # Populate library files tree (supported extensions only)
        supported_extensions = {".3mf", ".stl", ".obj", ".step", ".stp"}
        supported = [
            lf for lf in lib_files
            if Path(lf.get("filename") or lf.get("name", "")).suffix.lower()
            in supported_extensions
        ]
        self._lib_tree.clear()
        for lf in supported:
            fid     = lf.get("id")
            display = lf.get("_display_name", f"file_{fid}")
            status  = "synced" if fid in synced_file_ids else "new"
            checked = status != "synced"
            self._lib_tree.add_row(str(fid), display, status, checked)

        self._lib_label.configure(
            text=f"Library files  ({len(supported)} supported, "
                 f"{sum(1 for lf in supported if lf.get('id') in synced_file_ids)} already synced)"
        )

        if archives or lib_files:
            self._run_btn.configure(state="normal")

    # ── Sync execution ────────────────────────────────────────────────────────

    def _start_sync(self):
        if not self._archives and not self._lib_files:
            messagebox.showinfo("No models", "Click 'Load models' first.")
            return

        cfg = self._validate_config()
        if cfg is None:
            return

        selected_archive_ids = {int(iid) for iid in self._arch_tree.checked_iids()}
        selected_file_ids    = {int(iid) for iid in self._lib_tree.checked_iids()}

        if not selected_archive_ids and not selected_file_ids:
            messagebox.showinfo("Nothing selected", "Select at least one model to sync.")
            return

        dry_run = self._dry_run_var.get()
        create_missing = self._create_missing_var.get()
        force = self._force_var.get()
        self._running = True
        self._load_btn.configure(state="disabled")
        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._progress.start(12)

        threading.Thread(
            target=self._sync_worker,
            args=(cfg, dry_run, create_missing, force, selected_archive_ids, selected_file_ids),
            daemon=True,
        ).start()

    def _request_stop(self):
        self._running = False
        self._append_log("\n⚠️  Stop requested — will halt after the current file.\n")

    def _sync_done(self):
        self._running = False
        self._progress.stop()
        self._load_btn.configure(state="normal")
        self._run_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")

    def _sync_worker(
        self,
        cfg: dict,
        dry_run: bool,
        create_missing: bool,
        force: bool,
        selected_archive_ids: set[int],
        selected_file_ids: set[int],
    ):
        self._set_env(cfg)

        import importlib
        import bambuddy_to_manyfold as sync_mod
        importlib.reload(sync_mod)

        writer = _QueueWriter(self._log_queue)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = writer  # type: ignore[assignment]

        try:
            import requests as _requests
            import time

            import datetime
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{'─' * 60}")
            print(f"  🚀  Sync started at {ts}" + ("  [DRY RUN]" if dry_run else ""))
            print(f"{'─' * 60}\n")

            session = _requests.Session()
            sync_mod.check_connections(session)

            state          = sync_mod.load_sync_state()
            existing_names = sync_mod.get_existing_manyfold_models(session)
            print(f"  ℹ️  {len(existing_names)} existing models found in Manyfold.\n")

            start = time.time()
            archives_added = sync_mod.sync_archives(
                session, state, existing_names, dry_run,
                selected_ids=selected_archive_ids,
                create_missing=create_missing,
                force=force,
            )

            if not self._running:
                print("\n⚠️  Stopped before library sync.\n")
            else:
                library_added = sync_mod.sync_library_files(
                    session, state, existing_names, dry_run,
                    selected_ids=selected_file_ids,
                    create_missing=create_missing,
                    force=force,
                )

                if not dry_run:
                    sync_mod.save_sync_state(state)

                elapsed = time.time() - start
                print(f"\n✅ Sync complete in {elapsed:.1f}s")
                print(f"   Archives uploaded     : {archives_added}")
                print(f"   Library files uploaded: {library_added}")
                if dry_run:
                    print("   (dry-run — no changes were made)")

        except SystemExit as e:
            if str(e) != "0":
                print(f"\n❌ Aborted (exit code {e})\n")
        except Exception as e:
            print(f"\n❌ Unexpected error: {e}\n")
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
            self.after(0, self._sync_done)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
