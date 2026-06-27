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

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
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
    return {"synced_archives": [], "synced_library_files": [], "synced_library_folders": {}}


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


def _html_to_text(value: str | None) -> str | None:
    """Convert MakerWorld's HTML summary into plain text for the Manyfold notes.

    Keeps paragraph/line breaks, drops all other tags, unescapes entities.
    """
    if not value:
        return None
    text = re.sub(r"(?i)</p\s*>", "\n\n", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)        # strip remaining tags
    text = html.unescape(text)
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

def obtain_manyfold_token(session: requests.Session) -> bool:
    """Exchange client_credentials for an access token; update the global MANYFOLD_TOKEN.

    Only the client_credentials flow can mint a token carrying the 'upload'
    scope (personal access tokens can't), so this is the path used for syncing
    files. Returns True on success. No-op (returns True) if client credentials
    aren't configured — the caller then falls back to a pre-issued MANYFOLD_TOKEN.
    """
    global MANYFOLD_TOKEN
    if not (MANYFOLD_CLIENT_ID and MANYFOLD_CLIENT_SECRET):
        return True  # fall back to MANYFOLD_TOKEN

    print("  🔑 Requesting Manyfold token via client_credentials...")
    try:
        resp = session.post(
            f"{MANYFOLD_URL}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": MANYFOLD_CLIENT_ID,
                "client_secret": MANYFOLD_CLIENT_SECRET,
                "scope": MANYFOLD_SCOPES,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"  ❌ Failed to reach Manyfold token endpoint: {e}")
        return False

    if not resp.ok:
        print(f"  ❌ Token request failed: {resp.status_code} {resp.text[:200]}")
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


def add_manyfold_model_link(session: requests.Session, model_id: str, url: str, text: str) -> bool:
    """PATCH a link onto an existing Manyfold model (ModelDeserializer supports links)."""
    resp = session.patch(
        f"{MANYFOLD_URL}/models/{model_id}",
        json={"links": [{"url": url, "text": text}]},
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
    description: str | None = None,
    tag_list: list[str] | None = None,
) -> bool:
    """PATCH description (notes) and/or tags onto an existing Manyfold model."""
    payload: dict = {}
    if description:
        payload["description"] = description
    if tag_list:
        payload["keywords"] = tag_list
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


def add_image_to_manyfold_model(session: requests.Session, model_id: str, image_path: Path) -> bool:
    """Tus-upload an image and attach it to an existing Manyfold model as a file."""
    upload_url = tus_upload_file(session, image_path)
    if not upload_url:
        return False
    resp = session.post(
        f"{MANYFOLD_URL}/models/{model_id}/model_files",
        json={"files": [{"id": upload_url, "name": image_path.name}]},
        headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
        timeout=60,
    )
    if resp.status_code == 202:
        return True
    print(f"    ⚠️  Failed to add image to model {model_id}: {resp.status_code} {resp.text[:200]}")
    return False


def enrich_manyfold_model_from_makerworld(
    session: requests.Session,
    model_id: str,
    model_name: str,
    source_url: str,
) -> None:
    """Fetch MakerWorld design metadata and apply it to a Manyfold model.

    Best-effort: sets description + tags, and attaches the cover image. Any
    individual failure is logged (debug) and skipped — never raises.
    """
    design = get_makerworld_design(session, source_url)
    if not design:
        return

    description = _html_to_text(design.get("summary"))
    tags = _extract_makerworld_tags(design)
    if description or tags:
        if patch_manyfold_model_metadata(session, model_id, description=description, tag_list=tags):
            bits = []
            if description:
                bits.append("description")
            if tags:
                bits.append(f"{len(tags)} tag(s)")
            dprint(f"    📝 Set {', '.join(bits)}")

    cover_url = design.get("coverUrl") or design.get("cover")
    if cover_url:
        ext = _image_ext_from_url(cover_url)
        safe_name = re.sub(r"[^\w.-]+", "_", model_name)[:60] or "cover"
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dest = Path(tmpdir) / f"{safe_name}_cover{ext}"
            if download_makerworld_image(session, cover_url, img_dest):
                if add_image_to_manyfold_model(session, model_id, img_dest):
                    dprint("    🖼  Attached MakerWorld cover image")


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
) -> bool:
    """Create a model in Manyfold from a previously-uploaded Tus file.

    Manyfold creates models *from* uploaded files (async) — there is no
    "empty model" concept in the API. Returns True on 202 Accepted.

    If ``source_url`` is given, the model is located after the async creation
    job runs; with ``add_link`` the URL is PATCHed on as a link (the upload
    endpoint can't accept links directly), and with ``enrich`` the MakerWorld
    design metadata (description, tags, cover image) is fetched and applied.
    All of this is best-effort: a failure to find the model in time logs a
    warning but doesn't fail the sync.
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

    resp = session.post(
        f"{MANYFOLD_URL}/models",
        json=payload,
        headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
        timeout=60,
    )
    if resp.status_code != 202:
        print(f"    ⚠️  Model create failed for '{name}': {resp.status_code} {resp.text[:300]}")
        return False

    if need_lookup:
        model_id = _poll_for_new_manyfold_model(session, existing_ids)
        if model_id:
            if add_link and add_manyfold_model_link(session, model_id, source_url, source_text):
                dprint(f"    🔗 Linked {source_text}: {source_url}")
            if enrich and source_text == "MakerWorld":
                enrich_manyfold_model_from_makerworld(session, model_id, name, source_url)
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
) -> bool:
    """Tus-upload a file and create a model from it in Manyfold.

    Wraps the two-step Manyfold flow (tus upload → POST /models) so the
    sync loops have a single call site. Honours dry_run. When ``source_url``
    is given, ``add_link`` attaches it as a link and ``enrich`` applies the
    MakerWorld description/tags/cover image (both best-effort).
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
        source_url=source_url, source_text=source_text, add_link=add_link, enrich=enrich,
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
) -> int:
    if selected_ids is not None and len(selected_ids) == 0:
        print("\n📁 No library files selected — skipping.")
        return 0
    lib_files = get_bambuddy_library_files(session)
    if selected_ids is not None:
        lib_files = [f for f in lib_files if f.get("id") in selected_ids]
    # Map of library_file_id → MakerWorld source URL (for links + enrichment).
    # Enrichment implies we need the URLs too.
    need_urls = add_source_links or enrich_from_makerworld
    makerworld_urls = get_bambuddy_makerworld_urls(session) if need_urls else {}
    flat_folders = _flatten_folders(get_bambuddy_library_folders(session))
    # Map bambuddy folder_id → folder dict (with _full_path)
    folder_by_id: dict[int, dict] = {f["id"]: f for f in flat_folders}

    synced_ids: set = set(state.get("synced_library_files", []))
    # Map bambuddy folder_id → manyfold collection @id URL (JSON stores keys as strings)
    folder_to_collection: dict[int, str] = {int(k): v for k, v in state.get("synced_library_folders", {}).items()}

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

    for file_entry in tqdm(supported, unit="file"):
        file_id = file_entry.get("id")
        filename = file_entry.get("filename") or file_entry.get("name", f"file_{file_id}")
        model_name = filename
        while Path(model_name).suffix.lower() in STRIP_EXTS:
            model_name = Path(model_name).stem

        if file_id in synced_ids and not force:
            tqdm.write(f"  ⏭  Already synced: {model_name}")
            continue

        if model_name in existing_names:
            tqdm.write(f"  ⏭  Already in Manyfold (skipping duplicate): {model_name}")
            synced_ids.add(file_id)
            continue

        if not create_missing:
            tqdm.write(f"  ⏭  Not in Manyfold and create_missing=False — skipping: {model_name}")
            continue

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
                continue

            collection_at_id = _ensure_collection(folder_id)
            source_url = makerworld_urls.get(file_id) if (add_source_links or enrich_from_makerworld) else None
            if source_url:
                tqdm.write(f"  🔗 MakerWorld source: {source_url}")
            elif need_urls:
                dprint(f"    (no MakerWorld link for file id {file_id} — not in recent-imports window)")
            tqdm.write(f"  ↑  Uploading to Manyfold: {label}")
            ok = upload_model_to_manyfold(
                session, model_name, dest, collection_at_id, dry_run,
                source_url=source_url, source_text="MakerWorld",
                add_link=add_source_links, enrich=enrich_from_makerworld,
            )

        if ok:
            synced_ids.add(file_id)
            existing_names.add(model_name)
            new_count += 1

    state["synced_library_files"] = list(synced_ids)
    # Persist int keys as strings for JSON serialisation
    state["synced_library_folders"] = {str(k): v for k, v in folder_to_collection.items()}
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
    args = parser.parse_args()

    if args.dry_run:
        print("🔍 DRY RUN mode — nothing will be uploaded.\n")

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
    check_connections(session)

    state = load_sync_state()
    existing_names = get_existing_manyfold_models(session)
    print(f"  ℹ️  {len(existing_names)} existing models found in Manyfold.\n")

    start = time.time()
    create_missing = not args.no_create
    archives_added = sync_archives(session, state, existing_names, args.dry_run, create_missing=create_missing, force=args.force)
    library_added = sync_library_files(session, state, existing_names, args.dry_run, create_missing=create_missing, force=args.force, add_source_links=not args.no_links, enrich_from_makerworld=not args.no_enrich)

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
