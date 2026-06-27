#!/usr/bin/env python3
"""
bambuddy_to_manyfold.py
Syncs 3D model files from Bambuddy (archives + library) to a Manyfold instance.

Requirements:
    pip install requests tqdm

Usage:
    1. Fill in the CONFIG section below (or set environment variables).
    2. Run: python bambuddy_to_manyfold.py [--dry-run]

Bambuddy API docs:   http://<your-bambuddy>:8000/docs
Manyfold API docs:   http://<your-manyfold>/api

🤖 Built with Claude Code (https://claude.com/claude-code)
"""

import sys

# This script uses ``X | None`` type-hint syntax (PEP 604), which Python
# evaluates at import time and only supports on 3.10+. On older interpreters
# (notably macOS's bundled /usr/bin/python3, which is 3.9) the import would
# otherwise fail with a cryptic "unsupported operand type(s) for |" TypeError.
if sys.version_info < (3, 10):
    sys.exit(
        f"❌ Python 3.10+ required, but this is {sys.version.split()[0]} "
        f"({sys.executable}).\n"
        f"   macOS's /usr/bin/python3 is too old — use the python.org build, e.g.:\n"
        f"   python3.14 {' '.join(sys.argv) or 'bambuddy_to_manyfold.py'}"
    )

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import requests
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit here, or export as environment variables
# ─────────────────────────────────────────────────────────────────────────────

BAMBUDDY_URL = os.getenv("BAMBUDDY_URL", "http://localhost:8000")
BAMBUDDY_API_KEY = os.getenv("BAMBUDDY_API_KEY", "YOUR_BAMBUDDY_API_KEY")

MANYFOLD_URL = os.getenv("MANYFOLD_URL", "http://localhost:3214")
# Two ways to authenticate with Manyfold:
#
#   1. PREFERRED — OAuth client_credentials. Create an OAuth application in
#      Manyfold → Settings → API, grant it scopes "public read write upload",
#      and set MANYFOLD_CLIENT_ID / MANYFOLD_CLIENT_SECRET below. The script
#      exchanges these for a short-lived token automatically. Only this flow
#      can obtain the 'upload' scope needed to push files.
#
#   2. A pre-issued access token in MANYFOLD_TOKEN. NOTE: personal access
#      tokens generally CANNOT carry the 'upload' scope, so uploads will fail
#      — use the client_credentials flow above for syncing files.
MANYFOLD_CLIENT_ID = os.getenv("MANYFOLD_CLIENT_ID", "")
MANYFOLD_CLIENT_SECRET = os.getenv("MANYFOLD_CLIENT_SECRET", "")
MANYFOLD_TOKEN = os.getenv("MANYFOLD_TOKEN", "YOUR_MANYFOLD_OAUTH_TOKEN")

# Scopes requested via the client_credentials flow.
MANYFOLD_SCOPES = os.getenv("MANYFOLD_SCOPES", "public read write upload")

# Manyfold library ID to upload into (find it in the URL when browsing your library)
MANYFOLD_LIBRARY_ID = os.getenv("MANYFOLD_LIBRARY_ID", "1")

# How many items to fetch per page from Bambuddy
PAGE_SIZE = 50

# Supported file extensions to sync from the Bambuddy library
LIBRARY_EXTENSIONS = {".3mf", ".stl", ".obj", ".step", ".stp"}

# Path to a local JSON file that tracks already-synced items (prevents re-uploads)
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", "bambuddy_sync_state.json")

# Verbose diagnostic output (pagination, scope probe, etc.). Toggle via the
# MANYFOLD_SYNC_DEBUG env var ("1"/"true") or the GUI "Log debug" checkbox.
DEBUG = os.getenv("MANYFOLD_SYNC_DEBUG", "").lower() in ("1", "true", "yes", "on")


def dprint(*args, **kwargs):
    """print() that only fires when DEBUG is enabled."""
    if DEBUG:
        print(*args, **kwargs)

# ─────────────────────────────────────────────────────────────────────────────


def load_sync_state() -> dict:
    if Path(SYNC_STATE_FILE).exists():
        with open(SYNC_STATE_FILE) as f:
            return json.load(f)
    return {
        "synced_archives": [],
        "synced_library_files": [],
        "synced_library_folders": {},
        "synced_makerworld_models": {},
    }


def save_sync_state(state: dict):
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Bambuddy helpers ──────────────────────────────────────────────────────────

def bambuddy_headers() -> dict:
    return {"X-API-Key": BAMBUDDY_API_KEY, "Accept": "application/json"}


def get_bambuddy_archives(session: requests.Session) -> list:
    """Fetch all print archives (completed 3MF prints) from Bambuddy."""
    archives = []
    page = 1
    print("  Fetching Bambuddy archives...")
    while True:
        resp = session.get(
            f"{BAMBUDDY_URL}/api/v1/archives/",
            params={"page": page, "per_page": PAGE_SIZE, "status": "success"},
            headers=bambuddy_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Handle plain list or paginated dict
        if isinstance(data, list):
            batch = data
        else:
            batch = data.get("archives", data.get("items", []))
        archives.extend(batch)
        dprint(f"    Page {page}: {len(batch)} archives (total so far: {len(archives)})")
        total = data.get("total", len(archives)) if isinstance(data, dict) else len(archives)
        if not batch or len(archives) >= total:
            break
        page += 1
    return archives


def get_bambuddy_library_folders(session: requests.Session) -> list:
    """Fetch the full folder tree from the Bambuddy file manager."""
    print("  Fetching Bambuddy library folders...")
    resp = session.get(
        f"{BAMBUDDY_URL}/api/v1/library/folders",
        headers=bambuddy_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        print(f"    ⚠️  Unexpected folder response: {str(data)[:200]}")
        return []
    print(f"    {len(data)} root folder(s) found")
    return data


def _flatten_folders(folders: list, parent_path: str = "") -> list[dict]:
    """Recursively flatten the Bambuddy folder tree into a list with full path."""
    result = []
    for folder in folders:
        path = f"{parent_path}/{folder['name']}" if parent_path else folder["name"]
        result.append({**folder, "_full_path": path})
        children = folder.get("children", [])
        if children:
            result.extend(_flatten_folders(children, path))
    return result


def get_bambuddy_library_files(session: requests.Session) -> list:
    """Fetch all files from the Bambuddy file manager (all folders, not just root)."""
    print("  Fetching Bambuddy library files...")
    # include_root=False + no folder_id → returns every file regardless of folder
    resp = session.get(
        f"{BAMBUDDY_URL}/api/v1/library/files",
        params={"include_root": "false"},
        headers=bambuddy_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        print(f"    ⚠️  Library returned unexpected response: {str(data)[:200]}")
        return []
    print(f"    {len(data)} library file(s) found")
    return data


def get_bambuddy_makerworld_urls(session: requests.Session) -> dict[int, str]:
    """Return {library_file_id → MakerWorld source URL} for imported files.

    The MakerWorld import stores the source URL on the LibraryFile row, but it's
    NOT exposed by the general /library/files endpoints — only by the dedicated
    /makerworld/recent-imports endpoint, which is capped at 50 rows. Files beyond
    that cap simply won't get a link (logged, non-fatal).
    """
    mapping: dict[int, str] = {}
    try:
        resp = session.get(
            f"{BAMBUDDY_URL}/api/v1/makerworld/recent-imports",
            params={"limit": 50},
            headers=bambuddy_headers(),
            timeout=30,
        )
        if not resp.ok:
            dprint(f"    ⚠️  Could not fetch MakerWorld imports: {resp.status_code}")
            return mapping
        for row in resp.json():
            fid = row.get("library_file_id")
            url = row.get("source_url")
            if fid is not None and url:
                mapping[int(fid)] = url
    except Exception as e:
        dprint(f"    ⚠️  Could not fetch MakerWorld imports: {e}")
    if mapping:
        print(f"    {len(mapping)} MakerWorld source link(s) found (max 50)")
        dprint(f"    MakerWorld-linked file IDs: {sorted(mapping)}")
    return mapping


def get_makerworld_design(session: requests.Session, source_url: str) -> dict | None:
    """Resolve a MakerWorld URL to its design metadata via Bambuddy.

    Reuses Bambuddy's /makerworld/resolve endpoint (which handles MakerWorld's
    auth, anti-bot, and CDN quirks) rather than re-implementing the client.
    Returns the ``design`` dict (title, summary, coverUrl, license, tags, …) or
    None on any failure (non-fatal — enrichment is best-effort).
    """
    try:
        resp = session.post(
            f"{BAMBUDDY_URL}/api/v1/makerworld/resolve",
            json={"url": source_url},
            headers=bambuddy_headers(),
            timeout=45,
        )
        if not resp.ok:
            dprint(f"    ⚠️  MakerWorld resolve failed: {resp.status_code} {resp.text[:150]}")
            return None
        design = resp.json().get("design")
        return design if isinstance(design, dict) else None
    except Exception as e:
        dprint(f"    ⚠️  MakerWorld resolve error: {e}")
        return None


def download_makerworld_image(session: requests.Session, image_url: str, dest: Path) -> bool:
    """Download a MakerWorld CDN image via Bambuddy's thumbnail proxy."""
    try:
        resp = session.get(
            f"{BAMBUDDY_URL}/api/v1/makerworld/thumbnail",
            params={"url": image_url},
            headers=bambuddy_headers(),
            stream=True,
            timeout=60,
        )
        if not resp.ok:
            dprint(f"    ⚠️  Image download failed: {resp.status_code}")
            return False
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest.stat().st_size > 0
    except Exception as e:
        dprint(f"    ⚠️  Image download error: {e}")
        return False


class _HTMLToMarkdown(HTMLParser):
    """Minimal HTML→Markdown converter for MakerWorld design summaries.

    Manyfold renders model notes as Markdown, so converting (rather than
    flattening to plain text) preserves headings, emphasis, lists, links and
    images. Handles the common tags MakerWorld emits; unknown tags pass their
    text through.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: str | None = None
        self._list_stack: list[list] = []  # entries: ["ul"|"ol", counter]
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "br":
            self.out.append("  \n")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag == "a":
            self._href = a.get("href")
            self.out.append("[")
        elif tag == "img":
            src, alt = a.get("src", ""), a.get("alt", "")
            if src:
                self.out.append(f"![{alt}]({src})")
        elif tag in ("ul", "ol"):
            self._list_stack.append([tag, 0])
            self.out.append("\n")
        elif tag == "li":
            indent = "  " * max(0, len(self._list_stack) - 1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                self._list_stack[-1][1] += 1
                self.out.append(f"\n{indent}{self._list_stack[-1][1]}. ")
            else:
                self.out.append(f"\n{indent}- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p"):
            self.out.append("\n")
        elif tag == "a":
            href = self._href or ""
            self.out.append(f"]({href})" if href else "]")
            self._href = None
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_to_markdown(value: str | None) -> str | None:
    """Convert MakerWorld's HTML summary into Markdown for the Manyfold notes."""
    if not value:
        return None
    parser = _HTMLToMarkdown()
    parser.feed(value)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)     # trailing whitespace before newlines
    text = re.sub(r"\n{3,}", "\n\n", text)     # collapse excess blank lines
    return text.strip() or None


def _extract_makerworld_tags(design: dict) -> list[str]:
    """Pull a flat list of tag strings from the design's ``tags`` field, if any."""
    raw = design.get("tags")
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for t in raw:
        if isinstance(t, str) and t.strip():
            tags.append(t.strip())
        elif isinstance(t, dict):
            name = t.get("name") or t.get("title")
            if isinstance(name, str) and name.strip():
                tags.append(name.strip())
    return tags


def _image_ext_from_url(url: str) -> str:
    """Return a sane image extension from a URL path (default .png)."""
    path = urlsplit(url).path
    ext = Path(path).suffix.lower()
    return ext if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else ".png"


def _slugify(name: str) -> str:
    """Make a non-empty ASCII slug for a creator name.

    Manyfold rejects a blank slug (which its own parameterize produces for
    all-CJK / emoji names), so fall back to a hash when no Latin chars remain.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "creator-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return slug


def download_bambuddy_archive(session: requests.Session, archive_id: int, dest: Path):
    """Stream-download a Bambuddy archive 3MF to a local file."""
    resp = session.get(
        f"{BAMBUDDY_URL}/api/v1/archives/{archive_id}/download",
        headers=bambuddy_headers(),
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def download_bambuddy_library_file(session: requests.Session, file_id: int, dest: Path):
    """Stream-download a Bambuddy library file to a local path."""
    resp = session.get(
        f"{BAMBUDDY_URL}/api/v1/library/files/{file_id}/download",
        headers=bambuddy_headers(),
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


# ── Manyfold helpers ──────────────────────────────────────────────────────────

def obtain_manyfold_token(session: requests.Session, scopes: str | None = None) -> bool:
    """Exchange client_credentials for an access token; update the global MANYFOLD_TOKEN.

    Only the client_credentials flow can mint a token carrying the 'upload'
    scope (personal access tokens can't), so this is the path used for syncing
    files. ``scopes`` overrides the requested scope string (e.g. to add 'delete'
    for the cleanup command). Returns True on success. No-op (returns True) if
    client credentials aren't configured — the caller then falls back to a
    pre-issued MANYFOLD_TOKEN.
    """
    global MANYFOLD_TOKEN
    if not (MANYFOLD_CLIENT_ID and MANYFOLD_CLIENT_SECRET):
        return True  # fall back to MANYFOLD_TOKEN

    requested = scopes or MANYFOLD_SCOPES
    print("  🔑 Requesting Manyfold token via client_credentials...")
    try:
        resp = session.post(
            f"{MANYFOLD_URL}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": MANYFOLD_CLIENT_ID,
                "client_secret": MANYFOLD_CLIENT_SECRET,
                "scope": requested,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"  ❌ Failed to reach Manyfold token endpoint: {e}")
        return False

    if not resp.ok:
        print(f"  ❌ Token request failed: {resp.status_code} {resp.text[:200]}")
        if "invalid_scope" in resp.text and "delete" in requested:
            print("     Your OAuth application is missing a requested scope (likely 'delete').")
            print("     Add it in Manyfold → Settings → API, then retry.")
        return False

    data = resp.json()
    token = data.get("access_token")
    if not token:
        print(f"  ❌ Token response missing access_token: {str(data)[:200]}")
        return False

    granted = data.get("scope", "")
    MANYFOLD_TOKEN = token
    print(f"  ✅ Obtained Manyfold token (scopes: {granted or 'unknown'})")
    if "upload" not in granted.split():
        print("  ⚠️  Granted token does NOT include the 'upload' scope — uploads will fail.")
        print("     Grant 'upload' to the OAuth application in Manyfold → Settings → API.")
    return True


def manyfold_headers() -> dict:
    return {
        "Authorization": f"Bearer {MANYFOLD_TOKEN}",
        "Accept": "application/vnd.manyfold.v0+json",
    }


# Manyfold rate-limits model + model-file creation to 10 per 3 minutes and
# returns a bare 429 (no Retry-After). The limit is a fixed 3-minute window, so
# a burst can need most of 180s before the counter resets — retry long enough to
# ride out a full window rather than giving up partway.
RATELIMIT_RETRIES = 12
RATELIMIT_WAIT = 20.0


def manyfold_post(session: requests.Session, url: str, payload: dict, timeout: int = 120,
                  retries: int = RATELIMIT_RETRIES) -> requests.Response:
    """POST JSON to Manyfold, retrying on 429 rate-limit responses with backoff."""
    headers = {**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"}
    resp = session.post(url, json=payload, headers=headers, timeout=timeout)
    for attempt in range(retries):
        if resp.status_code != 429:
            return resp
        try:
            wait = float(resp.headers.get("Retry-After", RATELIMIT_WAIT))
        except (TypeError, ValueError):
            wait = RATELIMIT_WAIT
        wait = max(1.0, min(wait, 180.0))
        print(f"    ⏳ Rate limited (429); waiting {wait:.0f}s then retrying ({attempt + 1}/{retries})…")
        time.sleep(wait)
        resp = session.post(url, json=payload, headers=headers, timeout=timeout)
    return resp


# ── Cleanup: delete empty models ──────────────────────────────────────────────

def list_manyfold_models(session: requests.Session, collection_slug: str | None = None) -> list[tuple]:
    """Return [(model_id, name)] for all models, optionally filtered to a collection.

    Manyfold's model index supports ``?collection=<slug>`` (includes the
    collection's sub-collections), so we can scope the scan server-side.
    """
    out: list[tuple] = []
    page = 1
    while True:
        params = {"page": page}
        if collection_slug:
            params["collection"] = collection_slug
        resp = session.get(f"{MANYFOLD_URL}/models", params=params, headers=manyfold_headers(), timeout=30)
        if not resp.ok:
            print(f"  ⚠️  Could not list models: {resp.status_code} {resp.text[:150]}")
            break
        data = resp.json()
        members = data.get("member", [])
        for m in members:
            at_id = m.get("@id", "")
            mid = at_id.rstrip("/").split("/")[-1] if at_id else None
            if mid:
                out.append((mid, m.get("name", "")))
        total = data.get("totalItems", 0)
        if not members or len(out) >= total:
            break
        page += 1
    return out


def manyfold_model_file_count(session: requests.Session, model_id: str) -> int | None:
    """Return how many files a model has (len of hasPart), or None if unreadable."""
    try:
        resp = session.get(f"{MANYFOLD_URL}/models/{model_id}", headers=manyfold_headers(), timeout=20)
        if not resp.ok:
            return None
        return len(resp.json().get("hasPart", []) or [])
    except Exception:
        return None


def delete_manyfold_model(session: requests.Session, model_id: str) -> requests.Response:
    """DELETE a model. Requires the token to hold the 'delete' scope."""
    return session.delete(f"{MANYFOLD_URL}/models/{model_id}", headers=manyfold_headers(), timeout=30)


def cleanup_empty_models(
    session: requests.Session,
    collection_name: str | None,
    dry_run: bool,
) -> int:
    """Delete Manyfold models that have no files, optionally scoped to a collection.

    Targets models whose ``hasPart`` is empty — i.e. a model that was created but
    never got any file attached (e.g. an upload job that failed). A model with a
    cover image still counts as having a file and is left alone. Honours dry_run.
    Returns the number deleted (or that would be deleted in dry-run).
    """
    slug = None
    if collection_name:
        collections = get_existing_manyfold_collections(session)
        at_id = collections.get(collection_name)
        if not at_id:
            print(f"  ⚠️  Collection '{collection_name}' not found in Manyfold — nothing to do.")
            return 0
        slug = at_id.rstrip("/").split("/")[-1]

    scope_label = f"collection '{collection_name}'" if collection_name else "ALL collections"
    print(f"\n🧹 Scanning {scope_label} for models with no files...")
    models = list_manyfold_models(session, slug)
    print(f"  {len(models)} model(s) to check")

    empty: list[tuple] = []
    for mid, name in tqdm(models, unit="model"):
        count = manyfold_model_file_count(session, mid)
        if count == 0:
            empty.append((mid, name))
    print(f"  Found {len(empty)} empty model(s).")

    if dry_run:
        for mid, name in empty:
            print(f"    [dry-run] Would delete empty model: {name!r} ({mid})")
        if empty:
            print("   (dry-run — nothing deleted)")
        return len(empty)

    deleted = 0
    for mid, name in empty:
        resp = delete_manyfold_model(session, mid)
        if resp.status_code in (200, 204):
            print(f"    🗑  Deleted: {name!r}")
            deleted += 1
        elif resp.status_code in (401, 403):
            print(f"    ❌ Not permitted to delete (status {resp.status_code}).")
            print("       The token needs the 'delete' scope: add 'delete' to the OAuth")
            print("       application in Manyfold → Settings → API, and include it in MANYFOLD_SCOPES.")
            break
        else:
            print(f"    ⚠️  Failed to delete {name!r}: {resp.status_code} {resp.text[:150]}")
    return deleted


def get_existing_manyfold_models(session: requests.Session) -> set:
    """
    Return a set of model names already in Manyfold.
    Used to skip duplicates. Handles JSON-LD hydra:Collection format.
    """
    names = set()
    page = 1
    print("  Fetching existing Manyfold models...")
    while True:
        resp = session.get(
            f"{MANYFOLD_URL}/models",
            params={"page": page},
            headers=manyfold_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Manyfold returns JSON-LD with a "member" array
        members = data.get("member", [])
        for m in members:
            name = m.get("name", "")
            if name:
                names.add(name)
        total = data.get("totalItems", 0)
        dprint(f"    Page {page}: {len(members)} models (total so far: {len(names)}/{total})")
        if not members or len(names) >= total:
            break
        page += 1
    return names


TUS_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MiB per PATCH


def tus_upload_file(session: requests.Session, file_path: Path) -> str | None:
    """Upload a file to Manyfold's Tus endpoint and return the upload URL (tus id).

    Manyfold mounts a tus-ruby-server at ``/upload``. The flow is:
      1. POST /upload (creation) with Upload-Length + Upload-Metadata → 201 + Location
      2. PATCH <location> in chunks with Upload-Offset → 204 each
    The returned Location URL is what ``POST /models`` expects as the file ``id``.
    """
    import base64

    file_size = file_path.stat().st_size
    # Tus metadata: comma-separated "key base64(value)" pairs.
    filename_b64 = base64.b64encode(file_path.name.encode()).decode()
    metadata = f"filename {filename_b64}"

    # ── 1. Create the upload ──────────────────────────────────────────────────
    create_resp = session.post(
        f"{MANYFOLD_URL}/upload",
        headers={
            "Authorization": f"Bearer {MANYFOLD_TOKEN}",
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
            "Upload-Metadata": metadata,
        },
        timeout=30,
        allow_redirects=False,
    )
    if create_resp.status_code != 201:
        print(f"    ⚠️  Tus create failed: {create_resp.status_code} {create_resp.text[:200]}")
        return None

    location = create_resp.headers.get("Location", "")
    if not location:
        print("    ⚠️  Tus create returned no Location header")
        return None
    # Location may be relative (e.g. /upload/<id>) — resolve against the base URL.
    if location.startswith("/"):
        upload_url = f"{MANYFOLD_URL.rstrip('/')}{location}"
    elif location.startswith("http"):
        upload_url = location
    else:
        upload_url = f"{MANYFOLD_URL.rstrip('/')}/upload/{location}"

    # ── 2. Upload the bytes in chunks ─────────────────────────────────────────
    offset = 0
    with open(file_path, "rb") as f:
        while offset < file_size:
            chunk = f.read(TUS_CHUNK_SIZE)
            if not chunk:
                break
            patch_resp = session.patch(
                upload_url,
                headers={
                    "Authorization": f"Bearer {MANYFOLD_TOKEN}",
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                },
                data=chunk,
                timeout=300,
                allow_redirects=False,
            )
            if patch_resp.status_code != 204:
                print(f"    ⚠️  Tus PATCH failed at offset {offset}: "
                      f"{patch_resp.status_code} {patch_resp.text[:200]}")
                return None
            new_offset = int(patch_resp.headers.get("Upload-Offset", offset + len(chunk)))
            offset = new_offset

    return upload_url


def _get_all_manyfold_model_ids(session: requests.Session) -> set[str]:
    """Return the set of all model slugs/IDs currently in Manyfold."""
    ids: set[str] = set()
    page = 1
    while True:
        try:
            resp = session.get(
                f"{MANYFOLD_URL}/models",
                params={"page": page},
                headers=manyfold_headers(),
                timeout=15,
            )
            if not resp.ok:
                break
            data = resp.json()
            members = data.get("member", [])
            for m in members:
                at_id = m.get("@id", "")
                if at_id:
                    ids.add(at_id.rstrip("/").split("/")[-1])
            total = data.get("totalItems", 0)
            if not members or len(ids) >= total:
                break
            page += 1
        except Exception:
            break
    return ids


def _manyfold_model_link_urls(session: requests.Session, model_id: str) -> set[str]:
    """Return the set of URLs already linked on a Manyfold model."""
    try:
        resp = session.get(
            f"{MANYFOLD_URL}/models/{model_id}",
            headers=manyfold_headers(),
            timeout=20,
        )
        if resp.ok:
            return {l.get("url", "") for l in resp.json().get("links", []) if l.get("url")}
    except Exception:
        pass
    return set()


def add_manyfold_model_link(session: requests.Session, model_id: str, url: str, text: str | None = None) -> bool:
    """PATCH a link onto a Manyfold model, idempotently.

    PATCHing ``links`` appends (it doesn't replace), so without this guard a
    re-sync would pile up duplicate links. Skip if the URL is already linked.

    By default no link ``text`` is sent — Manyfold then renders the link using
    its own site-derived label (e.g. "MakerWorld"). Passing a custom text just
    ends up sitting next to that label and reads as a doubled "...MakerWorld".
    """
    if url in _manyfold_model_link_urls(session, model_id):
        dprint(f"    🔗 Link already present, skipping: {url}")
        return True
    link: dict = {"url": url}
    if text:
        link["text"] = text
    resp = session.patch(
        f"{MANYFOLD_URL}/models/{model_id}",
        json={"links": [link]},
        headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
        timeout=30,
    )
    if resp.ok:
        return True
    print(f"    ⚠️  Failed to add link to model {model_id}: {resp.status_code} {resp.text[:200]}")
    return False


def patch_manyfold_model_metadata(
    session: requests.Session,
    model_id: str,
    name: str | None = None,
    description: str | None = None,
    tag_list: list[str] | None = None,
    creator_at_id: str | None = None,
    preview_file_at_id: str | None = None,
) -> bool:
    """PATCH metadata fields onto an existing Manyfold model.

    Any combination of name, description (notes), tags (keywords), creator and
    preview_file. No-op (returns True) if nothing to set.
    """
    payload: dict = {}
    if name:
        payload["name"] = name
    if description:
        payload["description"] = description
    if tag_list:
        payload["keywords"] = tag_list
    if creator_at_id:
        payload["creator"] = {"@id": creator_at_id}
    if preview_file_at_id:
        payload["preview_file"] = {"@id": preview_file_at_id}
    if not payload:
        return True
    resp = session.patch(
        f"{MANYFOLD_URL}/models/{model_id}",
        json=payload,
        headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
        timeout=30,
    )
    if resp.ok:
        return True
    print(f"    ⚠️  Failed to set metadata on model {model_id}: {resp.status_code} {resp.text[:200]}")
    return False


# Lazily-populated {creator_name_lower → creator @id URL}. Reset each run because
# the GUI reloads the module; the CLI is a single run.
_MANYFOLD_CREATORS: dict[str, str] | None = None


def ensure_manyfold_creator(session: requests.Session, name: str) -> str | None:
    """Find a Manyfold creator by name (case-insensitive), creating it if absent.

    Returns the creator's @id URL, or None on failure.
    """
    global _MANYFOLD_CREATORS
    key = name.strip().lower()
    if not key:
        return None

    if _MANYFOLD_CREATORS is None:
        _MANYFOLD_CREATORS = {}
        page = 1
        while True:
            try:
                resp = session.get(
                    f"{MANYFOLD_URL}/creators",
                    params={"page": page},
                    headers=manyfold_headers(),
                    timeout=20,
                )
                if not resp.ok:
                    break
                data = resp.json()
                members = data.get("member", [])
                for c in members:
                    cname = c.get("name", "")
                    at_id = c.get("@id", "")
                    if cname and at_id:
                        _MANYFOLD_CREATORS[cname.strip().lower()] = at_id
                total = data.get("totalItems", 0)
                if not members or len(_MANYFOLD_CREATORS) >= total:
                    break
                page += 1
            except Exception:
                break

    if key in _MANYFOLD_CREATORS:
        return _MANYFOLD_CREATORS[key]

    # Create it (synchronous — returns 201 with the serialized creator @id).
    # Always send an explicit slug: Manyfold auto-derives one from the name via
    # parameterize, which yields a BLANK slug for all-CJK / non-Latin names and
    # then 422s ("slug can't be blank"). We generate an ASCII slug with a hash
    # fallback so any name works.
    try:
        resp = session.post(
            f"{MANYFOLD_URL}/creators",
            json={"name": name, "slug": _slugify(name)},
            headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            at_id = (resp.json() or {}).get("@id")
            if not at_id:
                loc = resp.headers.get("Location", "")
                at_id = f"{MANYFOLD_URL}{loc}" if loc.startswith("/") else loc or None
            if at_id:
                _MANYFOLD_CREATORS[key] = at_id
                return at_id
        else:
            dprint(f"    ⚠️  Failed to create creator '{name}': {resp.status_code} {resp.text[:150]}")
    except Exception as e:
        dprint(f"    ⚠️  Creator create error for '{name}': {e}")
    return None


def add_file_to_manyfold_model(session: requests.Session, model_id: str, file_path: Path) -> bool:
    """Tus-upload a file and attach it to an existing Manyfold model.

    Used both to add a MakerWorld cover image and to add sibling profile
    files when grouping related MakerWorld profiles into one model.
    """
    upload_url = tus_upload_file(session, file_path)
    if not upload_url:
        return False
    resp = manyfold_post(
        session,
        f"{MANYFOLD_URL}/models/{model_id}/model_files",
        {"files": [{"id": upload_url, "name": file_path.name}]},
        timeout=120,
    )
    if resp.status_code == 202:
        return True
    print(f"    ⚠️  Failed to add file to model {model_id}: {resp.status_code} {resp.text[:200]}")
    return False


def find_manyfold_model_image_file(
    session: requests.Session,
    model_id: str,
    attempts: int = 8,
    delay: float = 2.0,
) -> str | None:
    """Poll GET /models/{id} until an image file appears in hasPart; return its @id.

    Used to set the cover as the model's preview_file after the async
    model_files job has processed the uploaded image.
    """
    import time
    for _ in range(attempts):
        time.sleep(delay)
        try:
            resp = session.get(
                f"{MANYFOLD_URL}/models/{model_id}",
                headers=manyfold_headers(),
                timeout=20,
            )
            if not resp.ok:
                continue
            for part in resp.json().get("hasPart", []):
                fmt = (part.get("encodingFormat") or "").lower()
                if fmt.startswith("image/") and part.get("@id"):
                    return part["@id"]
        except Exception:
            pass
    return None


def enrich_manyfold_model_from_makerworld(
    session: requests.Session,
    model_id: str,
    model_name: str,
    source_url: str,
    design: dict | None = None,
) -> None:
    """Fetch MakerWorld design metadata and apply it to a Manyfold model.

    Best-effort: sets name, description (Markdown), tags, creator, and attaches
    the cover image — also setting it as the model's preview_file. Reuses a
    pre-fetched ``design`` when given (avoids a second resolve). Any individual
    failure is logged (debug) and skipped — never raises.
    """
    if design is None:
        design = get_makerworld_design(session, source_url)
    if not design:
        return

    title = (design.get("title") or "").strip() or None
    description = _html_to_markdown(design.get("summary"))
    tags = _extract_makerworld_tags(design)
    creator_name = (design.get("designCreator") or {}).get("name")
    creator_at_id = ensure_manyfold_creator(session, creator_name) if creator_name else None

    if patch_manyfold_model_metadata(
        session, model_id,
        name=title, description=description, tag_list=tags, creator_at_id=creator_at_id,
    ):
        bits = []
        if title:
            bits.append(f"name='{title}'")
        if description:
            bits.append("description")
        if tags:
            bits.append(f"{len(tags)} tag(s)")
        if creator_at_id:
            bits.append(f"creator='{creator_name}'")
        if bits:
            dprint(f"    📝 Set {', '.join(bits)}")

    cover_url = design.get("coverUrl") or design.get("cover")
    if cover_url:
        ext = _image_ext_from_url(cover_url)
        safe_name = re.sub(r"[^\w.-]+", "_", title or model_name)[:60] or "cover"
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dest = Path(tmpdir) / f"{safe_name}_cover{ext}"
            if download_makerworld_image(session, cover_url, img_dest) and \
                    add_file_to_manyfold_model(session, model_id, img_dest):
                dprint("    🖼  Attached MakerWorld cover image")
                # Set it as the preview once the async file job has processed it.
                preview_at_id = find_manyfold_model_image_file(session, model_id)
                if preview_at_id:
                    if patch_manyfold_model_metadata(session, model_id, preview_file_at_id=preview_at_id):
                        dprint("    ⭐ Set cover as preview file")
                else:
                    dprint("    (image added but couldn't locate it to set as preview)")


def create_manyfold_model_from_upload(
    session: requests.Session,
    name: str,
    upload_url: str,
    filename: str,
    collection_at_id: str | None = None,
    source_url: str | None = None,
    source_text: str = "Source",
    add_link: bool = True,
    enrich: bool = False,
    design: dict | None = None,
) -> bool:
    """Create a model in Manyfold from a previously-uploaded Tus file.

    Manyfold creates models *from* uploaded files (async) — there is no
    "empty model" concept in the API. Returns True on 202 Accepted.

    If ``source_url`` is given, the model is located after the async creation
    job runs; with ``add_link`` the URL is PATCHed on as a link (the upload
    endpoint can't accept links directly), and with ``enrich`` the MakerWorld
    design metadata (description, tags, creator, cover image) is applied. When
    ``design`` is provided it's reused (the caller already resolved it to name
    the model); otherwise enrich resolves it. All best-effort.
    """
    # Snapshot existing model IDs first so we can detect the newly-created one
    # by diff after the async job runs (needed to attach a link / enrich).
    need_lookup = bool(source_url) and (add_link or enrich)
    existing_ids = _get_all_manyfold_model_ids(session) if need_lookup else set()

    # Always send isPartOf — an EMPTY array when there's no collection.
    # Manyfold's ProcessUploadedFileJob crashes with "undefined method 'map'
    # for nil" if the collections key ends up nil, which happens when isPartOf
    # is omitted entirely (the deserializer drops the key, the controller then
    # passes collection_ids: nil). An empty array deserialises to [] instead,
    # which the job handles fine.
    payload: dict = {
        "name": name,
        "files": [{"id": upload_url, "name": filename}],
        "isPartOf": [{"@id": collection_at_id}] if collection_at_id else [],
    }

    resp = manyfold_post(session, f"{MANYFOLD_URL}/models", payload, timeout=60)
    if resp.status_code != 202:
        print(f"    ⚠️  Model create failed for '{name}': {resp.status_code} {resp.text[:300]}")
        return False

    if need_lookup:
        model_id = _poll_for_new_manyfold_model(session, existing_ids)
        if model_id:
            # No custom link text — Manyfold renders its own site-derived label
            # (e.g. "MakerWorld"). Sending our own text just doubles up next to it.
            if add_link and add_manyfold_model_link(session, model_id, source_url):
                dprint(f"    🔗 Linked {source_text}: {source_url}")
            if enrich and source_text == "MakerWorld":
                enrich_manyfold_model_from_makerworld(session, model_id, name, source_url, design=design)
        else:
            print(f"    ⚠️  Created '{name}' but couldn't locate it to attach the source link / details.")

    return True


def _poll_for_new_manyfold_model(
    session: requests.Session,
    existing_ids: set[str],
    attempts: int = 8,
    delay: float = 2.0,
) -> str | None:
    """Poll until a model ID appears that wasn't in existing_ids (the async-created one)."""
    import time
    for _ in range(attempts):
        time.sleep(delay)
        new_ids = _get_all_manyfold_model_ids(session) - existing_ids
        if new_ids:
            return next(iter(new_ids))
    return None


def _makerworld_model_id(source_url: str | None) -> str | None:
    """Extract the MakerWorld design id from a source URL.

    Profiles of the same design share the design id but differ in the
    ``#profileId-`` fragment, so the design id is the grouping key.
    """
    if not source_url:
        return None
    match = re.search(r"/models/(\d+)", source_url)
    return match.group(1) if match else None


def find_manyfold_model_id_by_name(session: requests.Session, name: str) -> str | None:
    """Return the slug/ID of a Manyfold model whose name matches (paginated search)."""
    target = name.strip().lower()
    page = 1
    while True:
        try:
            resp = session.get(
                f"{MANYFOLD_URL}/models",
                params={"page": page},
                headers=manyfold_headers(),
                timeout=20,
            )
            if not resp.ok:
                break
            data = resp.json()
            members = data.get("member", [])
            for mem in members:
                if mem.get("name", "").strip().lower() == target:
                    at_id = mem.get("@id", "")
                    if at_id:
                        return at_id.rstrip("/").split("/")[-1]
            total = data.get("totalItems", 0)
            if not members or page * len(members) >= total:
                break
            page += 1
        except Exception:
            break
    return None


def _tus_upload_many(session: requests.Session, items: list[tuple]) -> tuple[list[dict], list]:
    """Tus-upload each (file_id, path); return (file_refs, uploaded_file_ids).

    ``file_refs`` are ``{"id": tus_url, "name": filename}`` dicts ready for a
    Manyfold ``files: [...]`` payload. Files whose upload fails are skipped.
    """
    refs: list[dict] = []
    ok_ids: list = []
    for fid, path in items:
        url = tus_upload_file(session, path)
        if url:
            refs.append({"id": url, "name": path.name})
            ok_ids.append(fid)
        else:
            print(f"    ⚠️  Upload failed for {path.name} — skipping")
    return refs, ok_ids


def create_manyfold_model_with_files(
    session: requests.Session,
    name: str,
    items: list[tuple],
    collection_at_id: str | None,
) -> tuple[str | None, list]:
    """Create one Manyfold model containing ALL ``items`` in a single request.

    ``items`` is a list of ``(file_id, path)``. Because Manyfold treats model
    files (.3mf/.stl/…) as a single multi-file upload (only all-archive uploads
    split into separate models), one ``POST /models`` with ``files: [...]``
    yields one model holding every file — and counts as just ONE rate-limited
    request. Returns ``(model_id, synced_file_ids)`` or ``(None, [])``.
    """
    if not items:
        return None, []
    refs, ok_ids = _tus_upload_many(session, items)
    if not refs:
        return None, []

    existing_ids = _get_all_manyfold_model_ids(session)
    payload = {
        "name": name,
        "files": refs,
        "isPartOf": [{"@id": collection_at_id}] if collection_at_id else [],
    }
    resp = manyfold_post(session, f"{MANYFOLD_URL}/models", payload, timeout=120)
    if resp.status_code != 202:
        print(f"    ⚠️  Model create failed for '{name}': {resp.status_code} {resp.text[:300]}")
        return None, []

    model_id = _poll_for_new_manyfold_model(session, existing_ids)
    if not model_id:
        print(f"    ⚠️  Created '{name}' but couldn't locate it for details.")
        return None, []
    return model_id, ok_ids


def add_files_to_manyfold_model(session: requests.Session, model_id: str, items: list[tuple]) -> list:
    """Add ALL ``items`` (file_id, path) to an existing model in ONE request.

    Returns the list of file_ids successfully submitted (202), or [] on failure.
    """
    if not items:
        return []
    refs, ok_ids = _tus_upload_many(session, items)
    if not refs:
        return []
    resp = manyfold_post(
        session,
        f"{MANYFOLD_URL}/models/{model_id}/model_files",
        {"files": refs},
        timeout=120,
    )
    if resp.status_code == 202:
        return ok_ids
    print(f"    ⚠️  Failed to add files to model {model_id}: {resp.status_code} {resp.text[:200]}")
    return []


def apply_makerworld_extras(
    session: requests.Session,
    model_id: str,
    name: str,
    source_url: str | None,
    add_link: bool,
    enrich: bool,
    design: dict | None = None,
) -> None:
    """Attach the source link and/or MakerWorld enrichment to an existing model."""
    if not source_url:
        return
    if add_link and add_manyfold_model_link(session, model_id, source_url):
        dprint(f"    🔗 Linked: {source_url}")
    if enrich:
        enrich_manyfold_model_from_makerworld(session, model_id, name, source_url, design=design)



def get_existing_manyfold_collections(session: requests.Session) -> dict:
    """Return a dict of collection name → collection @id URL for existing Manyfold collections."""
    collections = {}
    page = 1
    print("  Fetching existing Manyfold collections...")
    while True:
        resp = session.get(
            f"{MANYFOLD_URL}/collections",
            params={"page": page},
            headers=manyfold_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        members = data.get("member", [])
        for m in members:
            name = m.get("name", "")
            at_id = m.get("@id", "")
            if name and at_id:
                collections[name] = at_id
        total = data.get("totalItems", 0)
        dprint(f"    Page {page}: {len(members)} collections (total so far: {len(collections)}/{total})")
        if not members or len(collections) >= total:
            break
        page += 1
    return collections


def create_manyfold_collection(session: requests.Session, name: str, parent_at_id: str | None = None) -> str | None:
    """Create a Manyfold collection and return its @id URL."""
    payload: dict = {"name": name}
    if parent_at_id:
        payload["isPartOf"] = {"@id": parent_at_id}
    resp = session.post(
        f"{MANYFOLD_URL}/collections",
        json=payload,
        headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
        timeout=30,
    )
    if not resp.ok:
        print(f"    ⚠️  Failed to create collection '{name}': {resp.status_code} {resp.text[:200]}")
        return None
    data = resp.json()
    return data.get("@id")


def upload_model_to_manyfold(
    session: requests.Session,
    model_name: str,
    file_path: Path,
    collection_at_id: str | None,
    dry_run: bool,
    source_url: str | None = None,
    source_text: str = "Source",
    add_link: bool = True,
    enrich: bool = False,
    design: dict | None = None,
) -> bool:
    """Tus-upload a file and create a model from it in Manyfold.

    Wraps the two-step Manyfold flow (tus upload → POST /models) so the
    sync loops have a single call site. Honours dry_run. When ``source_url``
    is given, ``add_link`` attaches it as a link and ``enrich`` applies the
    MakerWorld details (both best-effort). A pre-fetched ``design`` is reused.
    """
    if dry_run:
        note = ""
        if source_url:
            extras = [n for n, on in (("link", add_link), ("details", enrich)) if on]
            if extras:
                note = f" with {source_text} " + " + ".join(extras)
        print(f"    [dry-run] Would upload {file_path.name} and create model '{model_name}'{note}")
        return True

    upload_url = tus_upload_file(session, file_path)
    if not upload_url:
        return False
    return create_manyfold_model_from_upload(
        session, model_name, upload_url, file_path.name, collection_at_id,
        source_url=source_url, source_text=source_text, add_link=add_link,
        enrich=enrich, design=design,
    )


# ── Main sync logic ───────────────────────────────────────────────────────────

def sync_archives(
    session: requests.Session,
    state: dict,
    existing_names: set,
    dry_run: bool,
    selected_ids: set | None = None,
    create_missing: bool = True,
    force: bool = False,
) -> int:
    if selected_ids is not None and len(selected_ids) == 0:
        print("\n📦 No archives selected — skipping.")
        return 0
    archives = get_bambuddy_archives(session)
    if selected_ids is not None:
        archives = [a for a in archives if a.get("id") in selected_ids]
    synced_ids: set = set(state["synced_archives"])
    new_count = 0

    print(f"\n📦 Syncing {len(archives)} Bambuddy archives (create_missing={create_missing}, force={force})...")
    for archive in tqdm(archives, unit="archive"):
        archive_id = archive.get("id")
        name = archive.get("name") or archive.get("filename", f"archive_{archive_id}")
        STRIP_EXTS = {".gcode", ".3mf", ".stl", ".obj", ".step", ".stp"}
        model_name = name
        while Path(model_name).suffix.lower() in STRIP_EXTS:
            model_name = Path(model_name).stem

        if archive_id in synced_ids and not force:
            tqdm.write(f"  ⏭  Already synced: {model_name}")
            continue

        if model_name in existing_names:
            tqdm.write(f"  ⏭  Already in Manyfold (skipping duplicate): {model_name}")
            synced_ids.add(archive_id)
            continue

        if not create_missing:
            tqdm.write(f"  ⏭  Not in Manyfold and create_missing=False — skipping: {model_name}")
            continue

        tqdm.write(f"  ↓  Downloading: {model_name}")
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / f"{model_name}.3mf"
            try:
                download_bambuddy_archive(session, archive_id, dest)
            except Exception as e:
                tqdm.write(f"  ⚠️  Download failed for archive {archive_id}: {e}")
                continue

            tqdm.write(f"  ↑  Uploading to Manyfold: {model_name}")
            ok = upload_model_to_manyfold(session, model_name, dest, None, dry_run)

        if ok:
            synced_ids.add(archive_id)
            existing_names.add(model_name)
            new_count += 1

    state["synced_archives"] = list(synced_ids)
    return new_count


def sync_library_files(
    session: requests.Session,
    state: dict,
    existing_names: set,
    dry_run: bool,
    selected_ids: set | None = None,
    create_missing: bool = True,
    force: bool = False,
    add_source_links: bool = True,
    enrich_from_makerworld: bool = True,
    group_makerworld_profiles: bool = True,
) -> int:
    if selected_ids is not None and len(selected_ids) == 0:
        print("\n📁 No library files selected — skipping.")
        return 0
    lib_files = get_bambuddy_library_files(session)
    if selected_ids is not None:
        lib_files = [f for f in lib_files if f.get("id") in selected_ids]
    # Map of library_file_id → MakerWorld source URL (for links + enrichment +
    # grouping — all need the URLs).
    need_urls = add_source_links or enrich_from_makerworld or group_makerworld_profiles
    makerworld_urls = get_bambuddy_makerworld_urls(session) if need_urls else {}
    flat_folders = _flatten_folders(get_bambuddy_library_folders(session))
    # Map bambuddy folder_id → folder dict (with _full_path)
    folder_by_id: dict[int, dict] = {f["id"]: f for f in flat_folders}

    synced_ids: set = set(state.get("synced_library_files", []))
    # Map bambuddy folder_id → manyfold collection @id URL (JSON stores keys as strings)
    folder_to_collection: dict[int, str] = {int(k): v for k, v in state.get("synced_library_folders", {}).items()}
    # Map MakerWorld design id → manyfold model id (so later profile imports of the
    # same design get added to the existing model instead of creating a new one).
    synced_mw: dict[str, str] = dict(state.get("synced_makerworld_models", {}))

    new_count = 0

    # Filter to supported extensions
    STRIP_EXTS = {".gcode", ".3mf", ".stl", ".obj", ".step", ".stp"}
    supported = [
        f for f in lib_files
        if Path(f.get("filename", f.get("name", ""))).suffix.lower() in LIBRARY_EXTENSIONS
    ]
    print(f"\n📁 Syncing {len(supported)}/{len(lib_files)} Bambuddy library files (filtered by extension, create_missing={create_missing}, force={force})...")

    if not dry_run:
        existing_collections = get_existing_manyfold_collections(session)
    else:
        existing_collections = {}

    def _ensure_collection(folder_id: int | None) -> str | None:
        """Get or create the Manyfold collection for a Bambuddy folder, recursively."""
        if folder_id is None:
            return None
        if folder_id in folder_to_collection:
            return folder_to_collection[folder_id]

        folder = folder_by_id.get(folder_id)
        if not folder:
            return None

        # Ensure parent collection exists first
        parent_at_id = _ensure_collection(folder.get("parent_id"))
        col_name = folder["_full_path"]

        if col_name in existing_collections:
            at_id: str | None = existing_collections[col_name]
        elif dry_run:
            tqdm.write(f"  [dry-run] Would create collection '{col_name}'")
            return None
        else:
            at_id = create_manyfold_collection(session, col_name, parent_at_id)
            if at_id:
                existing_collections[col_name] = at_id

        if at_id:
            folder_to_collection[folder_id] = at_id
        return at_id

    def _name_from_filename(fn: str) -> str:
        n = fn
        while Path(n).suffix.lower() in STRIP_EXTS:
            n = Path(n).stem
        return n

    def process_single(file_entry: dict) -> int:
        """Sync one library file as its own Manyfold model. Returns 1 if synced."""
        file_id = file_entry.get("id")
        filename = file_entry.get("filename") or file_entry.get("name", f"file_{file_id}")
        model_name = _name_from_filename(filename)

        if file_id in synced_ids and not force:
            tqdm.write(f"  ⏭  Already synced: {model_name}")
            return 0

        source_url = makerworld_urls.get(file_id) if need_urls else None

        # Resolve the MakerWorld design BEFORE the name-dedup check, so the model
        # is named with (and de-duplicated against) the MakerWorld title from the
        # start — no post-create rename, so re-runs recognise it consistently.
        design = None
        if source_url and enrich_from_makerworld and not dry_run:
            design = get_makerworld_design(session, source_url)
            title = (design or {}).get("title")
            if title and title.strip():
                model_name = title.strip()

        if source_url:
            tqdm.write(f"  🔗 MakerWorld source: {source_url}")
        elif need_urls:
            dprint(f"    (no MakerWorld link for file id {file_id} — not in recent-imports window)")

        if model_name in existing_names:
            tqdm.write(f"  ⏭  Already in Manyfold (skipping duplicate): {model_name}")
            synced_ids.add(file_id)
            return 0

        if not create_missing:
            tqdm.write(f"  ⏭  Not in Manyfold and create_missing=False — skipping: {model_name}")
            return 0

        folder_id = file_entry.get("folder_id")
        folder_path = folder_by_id[folder_id]["_full_path"] if folder_id in folder_by_id else None
        label = f"{folder_path}/{model_name}" if folder_path else model_name

        tqdm.write(f"  ↓  Downloading: {label}")
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / filename
            try:
                download_bambuddy_library_file(session, file_id, dest)
            except Exception as e:
                tqdm.write(f"  ⚠️  Download failed for library file {file_id}: {e}")
                return 0

            collection_at_id = _ensure_collection(folder_id)
            tqdm.write(f"  ↑  Uploading to Manyfold: {label}")
            ok = upload_model_to_manyfold(
                session, model_name, dest, collection_at_id, dry_run,
                source_url=source_url, source_text="MakerWorld",
                add_link=add_source_links, enrich=enrich_from_makerworld,
                design=design,
            )

        if ok:
            synced_ids.add(file_id)
            existing_names.add(model_name)
            return 1
        return 0

    def process_group(mw_id: str, entries: list[dict]) -> int:
        """Sync a set of related MakerWorld profiles into a single Manyfold model.

        Returns the number of newly-synced files in the group.
        """
        to_sync = [e for e in entries if force or e.get("id") not in synced_ids]
        if not to_sync:
            tqdm.write(f"  ⏭  Already synced: MakerWorld design {mw_id} ({len(entries)} profile(s))")
            return 0

        rep_url = makerworld_urls.get(entries[0].get("id"))
        model_name = _name_from_filename(
            entries[0].get("filename") or entries[0].get("name", f"design_{mw_id}")
        )
        design = None
        if rep_url and enrich_from_makerworld and not dry_run:
            design = get_makerworld_design(session, rep_url)
            title = (design or {}).get("title")
            if title and title.strip():
                model_name = title.strip()

        tqdm.write(f"  🧩 MakerWorld design {mw_id}: {len(to_sync)} new profile(s) → '{model_name}'")
        if rep_url:
            tqdm.write(f"  🔗 MakerWorld source: {rep_url}")

        existing_model_id = synced_mw.get(mw_id)

        if dry_run:
            verb = "add to existing model" if existing_model_id else "create model"
            for e in to_sync:
                fn = e.get("filename") or e.get("name", "")
                tqdm.write(f"    [dry-run] Would {verb} '{model_name}' with {fn}")
            return len(to_sync)

        # Models not yet tracked but already present by title → reuse them.
        if not existing_model_id and model_name in existing_names:
            existing_model_id = find_manyfold_model_id_by_name(session, model_name)
            if existing_model_id:
                synced_mw[mw_id] = existing_model_id

        if not existing_model_id and not create_missing:
            tqdm.write(f"  ⏭  Not in Manyfold and create_missing=False — skipping: {model_name}")
            return 0

        folder_id = entries[0].get("folder_id")
        collection_at_id = _ensure_collection(folder_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Per-file subdir keeps the original filename (what Manyfold stores)
            # while avoiding collisions between profiles that share a filename.
            downloaded: list[tuple] = []  # (file_id, path)
            for e in to_sync:
                fid = e.get("id")
                fn = e.get("filename") or e.get("name", f"file_{fid}")
                sub = Path(tmpdir) / str(fid)
                sub.mkdir(parents=True, exist_ok=True)
                dest = sub / fn
                try:
                    download_bambuddy_library_file(session, fid, dest)
                    downloaded.append((fid, dest))
                except Exception as exc:
                    tqdm.write(f"  ⚠️  Download failed for library file {fid}: {exc}")
            if not downloaded:
                return 0

            # All files go in a SINGLE request (one rate-limited call) — Manyfold
            # accepts a multi-file create / model_files add.
            if existing_model_id:
                model_id = existing_model_id
                tqdm.write(f"  ＋ Adding {len(downloaded)} profile file(s) to existing model: {model_name}")
                synced_fids = add_files_to_manyfold_model(session, model_id, downloaded)
            else:
                tqdm.write(f"  ↑  Creating model '{model_name}' with {len(downloaded)} profile file(s)")
                model_id, synced_fids = create_manyfold_model_with_files(
                    session, model_name, downloaded, collection_at_id
                )
                if not model_id:
                    return 0
                apply_makerworld_extras(
                    session, model_id, model_name, rep_url,
                    add_source_links, enrich_from_makerworld, design,
                )
                existing_names.add(model_name)

        if not synced_fids:
            tqdm.write("  ⚠️  No files added this run — will retry on next sync")
            return 0
        synced_mw[mw_id] = model_id
        for fid in synced_fids:
            synced_ids.add(fid)
        missing = len(downloaded) - len(synced_fids)
        if missing:
            tqdm.write(f"  ⚠️  {missing} file(s) couldn't be added now — will retry on next sync")
        return len(synced_fids)

    # ── Build work items: MakerWorld groups (when enabled) + singletons ────────
    groups: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for f in supported:
        mw_id = _makerworld_model_id(makerworld_urls.get(f.get("id"))) if group_makerworld_profiles else None
        if mw_id:
            groups.setdefault(mw_id, []).append(f)
        else:
            singles.append(f)

    # Only treat as a "group" when a design actually has >1 profile; a lone
    # profile is simpler to handle on the singleton path.
    work: list[tuple[str, object]] = []
    for mw_id, entries in groups.items():
        if len(entries) > 1:
            work.append(("group", (mw_id, entries)))
        else:
            singles.append(entries[0])
    work.extend(("single", f) for f in singles)

    grouped_count = sum(1 for kind, _ in work if kind == "group")
    if grouped_count:
        print(f"  🧩 Grouping enabled: {grouped_count} MakerWorld design(s) with multiple profiles")

    for kind, payload in tqdm(work, unit="item"):
        if kind == "group":
            mw_id, entries = payload  # type: ignore[misc]
            new_count += process_group(mw_id, entries)
        else:
            new_count += process_single(payload)  # type: ignore[arg-type]

    state["synced_library_files"] = list(synced_ids)
    # Persist int keys as strings for JSON serialisation
    state["synced_library_folders"] = {str(k): v for k, v in folder_to_collection.items()}
    state["synced_makerworld_models"] = synced_mw
    return new_count


def check_connections(session: requests.Session):
    """Quick sanity-check that both services are reachable."""
    print("🔌 Checking connections...")

    # Bambuddy
    try:
        r = session.get(
            f"{BAMBUDDY_URL}/api/v1/system/info",
            headers=bambuddy_headers(),
            timeout=10,
        )
        r.raise_for_status()
        print(f"  ✅ Bambuddy reachable ({BAMBUDDY_URL})")
    except Exception as e:
        print(f"  ❌ Cannot reach Bambuddy at {BAMBUDDY_URL}: {e}")
        sys.exit(1)

    # Manyfold — obtain a client_credentials token first (if configured)
    if not obtain_manyfold_token(session):
        sys.exit(1)

    try:
        r = session.get(
            f"{MANYFOLD_URL}/models",
            params={"page": 1},
            headers=manyfold_headers(),
            timeout=10,
        )
        r.raise_for_status()
        print(f"  ✅ Manyfold reachable ({MANYFOLD_URL})")
    except Exception as e:
        print(f"  ❌ Cannot reach Manyfold at {MANYFOLD_URL}: {e}")
        sys.exit(1)

    # Manyfold OAuth token scope check (does the token have the 'upload' scope?)
    check_manyfold_upload_scope(session)


def check_manyfold_upload_scope(session: requests.Session):
    """Verify the Manyfold token has the 'upload' scope.

    Manyfold gates the Tus '/upload' endpoint on the 'upload' OAuth scope
    (separate from 'write', which only covers model/collection creation).
    We probe by creating a throwaway zero-length Tus upload and immediately
    deleting it. A 401/403 here means the token is missing 'upload' — the
    sync would otherwise fail silently on every file. See the README for
    how to grant 'public read write upload' to the OAuth application.
    """
    import base64
    try:
        resp = session.post(
            f"{MANYFOLD_URL}/upload",
            headers={
                "Authorization": f"Bearer {MANYFOLD_TOKEN}",
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "0",
                "Upload-Metadata": "filename " + base64.b64encode(b".scope_probe").decode(),
            },
            timeout=10,
            allow_redirects=False,
        )
    except Exception as e:
        print(f"  ⚠️  Could not verify Manyfold upload scope: {e}")
        return

    if resp.status_code == 201:
        dprint("  ✅ Manyfold token has 'upload' scope")
        # Clean up the throwaway upload (Tus termination extension).
        location = resp.headers.get("Location", "")
        if location:
            if location.startswith("/"):
                cleanup_url = f"{MANYFOLD_URL.rstrip('/')}{location}"
            elif location.startswith("http"):
                cleanup_url = location
            else:
                cleanup_url = f"{MANYFOLD_URL.rstrip('/')}/upload/{location}"
            try:
                session.delete(
                    cleanup_url,
                    headers={
                        "Authorization": f"Bearer {MANYFOLD_TOKEN}",
                        "Tus-Resumable": "1.0.0",
                    },
                    timeout=10,
                )
            except Exception:
                pass  # Leftover zero-byte upload is harmless; tus reaps it.
    elif resp.status_code in (401, 403):
        print(f"  ❌ Manyfold token is missing the 'upload' scope (got {resp.status_code}).")
        print("     The OAuth application needs scopes: public read write upload")
        print("     Fix it in Manyfold → Settings → API, then regenerate the token.")
        sys.exit(1)
    else:
        print(f"  ⚠️  Unexpected response probing upload scope: {resp.status_code} {resp.text[:150]}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync 3D models from Bambuddy to Manyfold."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without uploading anything.",
    )
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Skip models that don't already exist in Manyfold (never create new ones).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the local sync-state file and re-process items (recovers failed uploads).",
    )
    parser.add_argument(
        "--no-links",
        action="store_true",
        help="Do not attach MakerWorld source URLs as links on synced models.",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Do not fetch MakerWorld details (description, tags, cover image) for synced models.",
    )
    parser.add_argument(
        "--no-group",
        action="store_true",
        help="Do not group multiple MakerWorld profiles of the same design into one model.",
    )
    parser.add_argument(
        "--cleanup-empty",
        nargs="?",
        const="MakerWorld",
        metavar="COLLECTION",
        help="Instead of syncing, delete Manyfold models that have no files. Optionally "
             "limit to a collection by name (default 'MakerWorld'); pass 'ALL' to scan "
             "every collection. Honours --dry-run. Requires the 'delete' OAuth scope.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("🔍 DRY RUN mode — nothing will be changed.\n")

    # Validate config
    missing = []
    if BAMBUDDY_API_KEY == "YOUR_BAMBUDDY_API_KEY":
        missing.append("BAMBUDDY_API_KEY")
    # Need EITHER client_credentials OR a pre-issued token.
    has_client_creds = bool(MANYFOLD_CLIENT_ID and MANYFOLD_CLIENT_SECRET)
    has_token = MANYFOLD_TOKEN not in ("", "YOUR_MANYFOLD_OAUTH_TOKEN")
    if not has_client_creds and not has_token:
        missing.append("MANYFOLD_CLIENT_ID + MANYFOLD_CLIENT_SECRET (or MANYFOLD_TOKEN)")
    if missing:
        print(f"❌ Please set the following before running: {', '.join(missing)}")
        print("   Edit the CONFIG section at the top of this file, or export as env vars.")
        sys.exit(1)

    session = requests.Session()

    # ── Cleanup mode: delete empty models, then exit ──────────────────────────
    if args.cleanup_empty is not None:
        # Need a token that includes the 'delete' scope.
        scopes = MANYFOLD_SCOPES if "delete" in MANYFOLD_SCOPES.split() else f"{MANYFOLD_SCOPES} delete"
        print("🔌 Connecting to Manyfold for cleanup...")
        if not obtain_manyfold_token(session, scopes=scopes):
            sys.exit(1)
        collection = None if args.cleanup_empty.upper() == "ALL" else args.cleanup_empty
        deleted = cleanup_empty_models(session, collection, args.dry_run)
        print(f"\n✅ Cleanup complete — {deleted} empty model(s) "
              f"{'would be ' if args.dry_run else ''}deleted.")
        return

    check_connections(session)

    state = load_sync_state()
    existing_names = get_existing_manyfold_models(session)
    print(f"  ℹ️  {len(existing_names)} existing models found in Manyfold.\n")

    start = time.time()
    create_missing = not args.no_create
    archives_added = sync_archives(session, state, existing_names, args.dry_run, create_missing=create_missing, force=args.force)
    library_added = sync_library_files(session, state, existing_names, args.dry_run, create_missing=create_missing, force=args.force, add_source_links=not args.no_links, enrich_from_makerworld=not args.no_enrich, group_makerworld_profiles=not args.no_group)

    if not args.dry_run:
        save_sync_state(state)

    elapsed = time.time() - start
    print(f"\n✅ Sync complete in {elapsed:.1f}s")
    print(f"   Archives uploaded : {archives_added}")
    print(f"   Library files uploaded: {library_added}")
    if args.dry_run:
        print("   (dry-run — no changes were made)")


if __name__ == "__main__":
    main()
