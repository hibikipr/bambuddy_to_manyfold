#!/usr/bin/env python3
"""
bambuddy_to_manyfold_gui.py
Tkinter GUI wrapper for bambuddy_to_manyfold.py.

Requirements:
    pip install requests tqdm
    (tkinter is included with Python on macOS/Windows; on Linux: sudo apt install python3-tk)

Usage:
    python bambuddy_to_manyfold_gui.py

🤖 Built with Claude Code (https://claude.com/claude-code)
"""

import base64
import io
import json
import os
import queue
import struct
import sys
import threading
import tkinter as tk
import zlib
from pathlib import Path
from tkinter import filedialog, font, messagebox, scrolledtext, ttk


# ── App icon ──────────────────────────────────────────────────────────────────

def _png_chunk(typ: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def _make_icon_png(size: int = 64) -> bytes:
    """Render an isometric 3D cube (a "model") as RGBA PNG bytes — pure stdlib.

    The cube nods at what the app does: pushing 3D models from Bambuddy to
    Manyfold. Three shaded faces give a clean depth read at small sizes.
    """
    s = size
    px = bytearray(s * s * 4)  # transparent RGBA

    # Cube faces as parallelograms (origin + two edge vectors), scaled to `size`.
    f = s / 64.0
    T = (32 * f, 8 * f)
    L = (8 * f, 22 * f)
    R = (56 * f, 22 * f)
    B = (32 * f, 36 * f)
    side = 24 * f  # vertical drop of the side faces

    faces = [
        # (origin, u, v, colour)  — top (lightest), left (mid), right (darkest)
        (T, (L[0] - T[0], L[1] - T[1]), (R[0] - T[0], R[1] - T[1]), (108, 182, 255)),
        (L, (0, side), (B[0] - L[0], B[1] - L[1]), (58, 120, 194)),
        (R, (0, side), (B[0] - R[0], B[1] - R[1]), (43, 90, 147)),
    ]

    for y in range(s):
        for x in range(s):
            cx, cy = x + 0.5, y + 0.5
            for (ox, oy), (ux, uy), (vx, vy), (cr, cg, cb) in faces:
                det = ux * vy - uy * vx
                if det == 0:
                    continue
                a = ((cx - ox) * vy - (cy - oy) * vx) / det
                b = (ux * (cy - oy) - uy * (cx - ox)) / det
                if -0.02 <= a <= 1.02 and -0.02 <= b <= 1.02:
                    i = (y * s + x) * 4
                    px[i], px[i + 1], px[i + 2], px[i + 3] = cr, cg, cb, 255
                    break

    raw = bytearray()
    for y in range(s):
        raw.append(0)  # filter type 0 per scanline
        raw.extend(px[y * s * 4:(y + 1) * s * 4])
    idat = zlib.compress(bytes(raw), 9)
    ihdr = struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")

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
    A ttk.Treeview with virtual per-row checkboxes, backed by a data model so
    rows can be sorted (name/date) and filtered (hide synced) without losing
    check state.

    Columns: "check" (30 px), "name" (stretch), "date" (90 px), "status" (70 px).
    Clicking any cell on a row toggles its checkbox.
    """

    def __init__(self, parent: tk.Widget):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)

        self._tree = ttk.Treeview(
            frame,
            columns=("check", "name", "date", "status"),
            show="headings",
            selectmode="none",
        )
        self._tree.heading("check",  text="")
        self._tree.heading("name",   text="Name",   command=lambda: self._header_sort("name"))
        self._tree.heading("date",   text="Date",   command=lambda: self._header_sort("date"))
        self._tree.heading("status", text="Status", command=lambda: self._header_sort("status"))
        self._tree.column("check",  width=30,  stretch=False, anchor="center")
        self._tree.column("name",   stretch=True,             anchor="w")
        self._tree.column("date",   width=90,  stretch=False, anchor="center")
        self._tree.column("status", width=70,  stretch=False, anchor="center")

        sb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.tag_configure("synced", foreground="#888888")
        self._tree.bind("<Button-1>", self._on_click)

        # iid → {name, date, status, checked}
        self._rows: dict[str, dict] = {}
        self._order: list[str] = []          # insertion order
        self._sort_key = "name"
        self._sort_reverse = False
        self._hide_synced = False

    # ── Public API ────────────────────────────────────────────────────────────

    def clear(self):
        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        self._order.clear()

    def add_row(self, iid: str, name: str, status: str, checked: bool = True, date: str = ""):
        self._rows[iid] = {"name": name, "date": date or "", "status": status, "checked": checked}
        self._order.append(iid)
        # Defer rendering; caller calls render() once after bulk-adding.

    def render(self):
        """Rebuild the visible tree from the data model (applies sort + filter)."""
        self._tree.delete(*self._tree.get_children())
        visible = [iid for iid in self._order
                   if not (self._hide_synced and self._rows[iid]["status"] == "synced")]

        def key(iid):
            r = self._rows[iid]
            if self._sort_key == "date":
                return (r["date"], r["name"].lower())
            if self._sort_key == "status":
                return (r["status"], r["name"].lower())
            return r["name"].lower()

        visible.sort(key=key, reverse=self._sort_reverse)
        for iid in visible:
            r = self._rows[iid]
            tags = ("synced",) if r["status"] == "synced" else ()
            self._tree.insert(
                "", "end", iid=iid,
                values=(CHECK_ON if r["checked"] else CHECK_OFF, r["name"],
                        self._fmt_date(r["date"]), r["status"]),
                tags=tags,
            )

    def set_sort(self, key: str, reverse: bool = False):
        self._sort_key = key if key in ("name", "date", "status") else "name"
        self._sort_reverse = reverse
        self.render()

    def set_hide_synced(self, hide: bool):
        self._hide_synced = hide
        self.render()

    def set_all(self, checked: bool):
        # Only affects currently-visible rows.
        for iid in self._tree.get_children():
            self._rows[iid]["checked"] = checked
            self._tree.set(iid, "check", CHECK_ON if checked else CHECK_OFF)

    def checked_iids(self) -> list[str]:
        return [iid for iid, r in self._rows.items() if r["checked"]]

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_date(value: str) -> str:
        # ISO timestamp → YYYY-MM-DD for display; pass through anything else.
        return value[:10] if len(value) >= 10 else value

    def _header_sort(self, key: str):
        # Toggle direction if re-clicking the same column.
        if self._sort_key == key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key = key
            self._sort_reverse = False
        self.render()

    def _on_click(self, event: tk.Event):
        # Ignore clicks on the header row.
        if self._tree.identify_region(event.x, event.y) == "heading":
            return
        iid = self._tree.identify_row(event.y)
        if not iid or iid not in self._rows:
            return
        new_state = not self._rows[iid]["checked"]
        self._rows[iid]["checked"] = new_state
        self._tree.set(iid, "check", CHECK_ON if new_state else CHECK_OFF)


# ── Main window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bambuddy → Manyfold Sync")
        self.resizable(True, True)
        self.minsize(720, 640)
        self._set_app_icon()

        self._cfg = load_gui_config()
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._running = False

        # Raw data loaded by "Load models"
        self._archives: list[dict] = []
        self._lib_files: list[dict] = []

        self._build_ui()
        self._load_fields()
        self._poll_log()

    def _set_app_icon(self):
        """Set the window icon to a generated 3D-cube graphic (best-effort)."""
        try:
            png = _make_icon_png(64)
            # Keep a reference on the instance so Tk doesn't garbage-collect it.
            self._icon_img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            self.iconphoto(True, self._icon_img)
        except Exception:
            pass  # Older Tk without PNG support, etc. — non-fatal.

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

        self._links_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top_btn_frame, text="Add MakerWorld links", variable=self._links_var
        ).pack(side="left", padx=(0, 12))

        self._enrich_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top_btn_frame, text="Fetch MakerWorld details (description + cover)",
            variable=self._enrich_var
        ).pack(side="left", padx=(0, 12))

        self._group_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top_btn_frame, text="Group MakerWorld profiles into one model",
            variable=self._group_var
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
        sel_outer = ttk.LabelFrame(paned, text="Model selection  —  click a row to toggle (or a column header to sort)")
        paned.add(sel_outer, weight=2)

        # Sort + filter controls (apply to both lists)
        view_bar = ttk.Frame(sel_outer)
        view_bar.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(view_bar, text="Sort by:").pack(side="left")
        self._sort_var = tk.StringVar(value="Name")
        sort_combo = ttk.Combobox(
            view_bar, textvariable=self._sort_var, state="readonly", width=10,
            values=("Name", "Date", "Status"),
        )
        sort_combo.pack(side="left", padx=(4, 6))
        sort_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_view())

        self._sort_desc_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            view_bar, text="Descending", variable=self._sort_desc_var,
            command=self._apply_view,
        ).pack(side="left", padx=(0, 12))

        self._hide_synced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            view_bar, text="Hide already-synced", variable=self._hide_synced_var,
            command=self._apply_view,
        ).pack(side="left")

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
            date    = a.get("created_at") or a.get("created") or ""
            self._arch_tree.add_row(str(aid), stem, status, checked, date=date)

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
            date    = lf.get("created_at") or lf.get("created") or ""
            self._lib_tree.add_row(str(fid), display, status, checked, date=date)

        self._lib_label.configure(
            text=f"Library files  ({len(supported)} supported, "
                 f"{sum(1 for lf in supported if lf.get('id') in synced_file_ids)} already synced)"
        )

        # Apply the current sort/filter and render both trees.
        self._apply_view()

        if archives or lib_files:
            self._run_btn.configure(state="normal")

    def _apply_view(self):
        """Push the current sort key / direction / hide-synced setting to both lists."""
        key = {"Name": "name", "Date": "date", "Status": "status"}.get(self._sort_var.get(), "name")
        reverse = self._sort_desc_var.get()
        hide = self._hide_synced_var.get()
        for tree in (self._arch_tree, self._lib_tree):
            tree._hide_synced = hide
            tree._sort_key = key
            tree._sort_reverse = reverse
            tree.render()

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
        add_links = self._links_var.get()
        enrich = self._enrich_var.get()
        group = self._group_var.get()
        # Refresh the lists afterwards (unless it was a dry run — nothing changed).
        self._reload_after_sync = not dry_run
        self._running = True
        self._load_btn.configure(state="disabled")
        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._progress.start(12)

        threading.Thread(
            target=self._sync_worker,
            args=(cfg, dry_run, create_missing, force, add_links, enrich, group, selected_archive_ids, selected_file_ids),
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
        # Auto-reload so the lists reflect the new synced statuses.
        if getattr(self, "_reload_after_sync", False):
            self._reload_after_sync = False
            self._append_log("\n🔄 Reloading models to refresh statuses…\n")
            self.after(500, self._start_load)

    def _sync_worker(
        self,
        cfg: dict,
        dry_run: bool,
        create_missing: bool,
        force: bool,
        add_links: bool,
        enrich: bool,
        group: bool,
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
                    add_source_links=add_links,
                    enrich_from_makerworld=enrich,
                    group_makerworld_profiles=group,
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
