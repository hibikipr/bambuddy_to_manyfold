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
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit here, or export as environment variables
# ─────────────────────────────────────────────────────────────────────────────

BAMBUDDY_URL = os.getenv("BAMBUDDY_URL", "http://localhost:8000")
BAMBUDDY_API_KEY = os.getenv("BAMBUDDY_API_KEY", "YOUR_BAMBUDDY_API_KEY")

MANYFOLD_URL = os.getenv("MANYFOLD_URL", "http://localhost:3214")
# Create an OAuth application in Manyfold → Settings → OAuth Applications,
# then use the Client Credentials grant to get a token, or paste a token directly.
MANYFOLD_TOKEN = os.getenv("MANYFOLD_TOKEN", "YOUR_MANYFOLD_OAUTH_TOKEN")

# Manyfold library ID to upload into (find it in the URL when browsing your library)
MANYFOLD_LIBRARY_ID = os.getenv("MANYFOLD_LIBRARY_ID", "1")

# How many items to fetch per page from Bambuddy
PAGE_SIZE = 50

# Supported file extensions to sync from the Bambuddy library
LIBRARY_EXTENSIONS = {".3mf", ".stl", ".obj", ".step", ".stp"}

# Path to a local JSON file that tracks already-synced items (prevents re-uploads)
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", "bambuddy_sync_state.json")

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
        print(f"    Page {page}: {len(batch)} archives (total so far: {len(archives)})")
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
        print(f"    Page {page}: {len(members)} models (total so far: {len(names)}/{total})")
        if not members or len(names) >= total:
            break
        page += 1
    return names


def create_manyfold_model(session: requests.Session, name: str, collection_at_id: str | None = None):
    """Create an empty model in Manyfold and return its slug/ID."""
    payload: dict = {
        "name": name,
        "library_id": MANYFOLD_LIBRARY_ID,
    }
    if collection_at_id:
        payload["isPartOf"] = [{"@id": collection_at_id}]
    resp = session.post(
        f"{MANYFOLD_URL}/models",
        json=payload,
        headers={**manyfold_headers(), "Content-Type": "application/vnd.manyfold.v0+json"},
        timeout=30,
    )
    if not resp.ok:
        print(f"    ⚠️  Failed to create model '{name}': {resp.status_code} {resp.text[:200]}")
        return None
    data = resp.json()
    # Manyfold returns JSON-LD; ID is in "@id" as a URL path like /models/abc123
    at_id = data.get("@id", "")
    model_id = at_id.rstrip("/").split("/")[-1] if at_id else None
    return model_id



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
        print(f"    Page {page}: {len(members)} collections (total so far: {len(collections)}/{total})")
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


def upload_file_to_manyfold_model(
    session: requests.Session,
    model_id: str,
    file_path: Path,
    dry_run: bool,
):
    """Upload a file into an existing Manyfold model."""
    if dry_run:
        print(f"    [dry-run] Would upload {file_path.name} → model {model_id}")
        return True

    with open(file_path, "rb") as f:
        resp = session.post(
            f"{MANYFOLD_URL}/upload",
            headers={
                "Authorization": f"Bearer {MANYFOLD_TOKEN}",
                "Accept": "application/vnd.manyfold.v0+json",
            },
            files={"file": (file_path.name, f)},
            data={
                "model_id": model_id,
            },
            timeout=300,
        )
    if resp.ok:
        return True
    print(f"    ⚠️  Upload failed for {file_path.name}: {resp.status_code} {resp.text[:300]}")
    return False


# ── Main sync logic ───────────────────────────────────────────────────────────

def sync_archives(
    session: requests.Session,
    state: dict,
    existing_names: set,
    dry_run: bool,
) -> int:
    archives = get_bambuddy_archives(session)
    synced_ids: set = set(state["synced_archives"])
    new_count = 0

    print(f"\n📦 Syncing {len(archives)} Bambuddy archives...")
    for archive in tqdm(archives, unit="archive"):
        archive_id = archive.get("id")
        name = archive.get("name") or archive.get("filename", f"archive_{archive_id}")
        # Strip all known print/slicer extensions from the model name
        STRIP_EXTS = {".gcode", ".3mf", ".stl", ".obj", ".step", ".stp"}
        model_name = name
        while Path(model_name).suffix.lower() in STRIP_EXTS:
            model_name = Path(model_name).stem

        if archive_id in synced_ids:
            tqdm.write(f"  ⏭  Already synced: {model_name}")
            continue

        if model_name in existing_names:
            tqdm.write(f"  ⏭  Already in Manyfold (skipping duplicate): {model_name}")
            synced_ids.add(archive_id)
            continue

        tqdm.write(f"  ↓  Downloading: {model_name}")
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / f"{model_name}.3mf"
            try:
                download_bambuddy_archive(session, archive_id, dest)
            except Exception as e:
                tqdm.write(f"  ⚠️  Download failed for archive {archive_id}: {e}")
                continue

            if not dry_run:
                model_id = create_manyfold_model(session, model_name)
                if not model_id:
                    continue
                tqdm.write(f"  ↑  Uploading to Manyfold: {model_name}")
                ok = upload_file_to_manyfold_model(session, model_id, dest, dry_run)
            else:
                tqdm.write(f"  [dry-run] Would create model '{model_name}' and upload {dest.name}")
                ok = True

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
) -> int:
    lib_files = get_bambuddy_library_files(session)
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
    print(f"\n📁 Syncing {len(supported)}/{len(lib_files)} Bambuddy library files (filtered by extension)...")

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

        if file_id in synced_ids:
            tqdm.write(f"  ⏭  Already synced: {model_name}")
            continue

        if model_name in existing_names:
            tqdm.write(f"  ⏭  Already in Manyfold (skipping duplicate): {model_name}")
            synced_ids.add(file_id)
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

            if not dry_run:
                collection_at_id = _ensure_collection(folder_id)
                model_id = create_manyfold_model(session, model_name, collection_at_id)
                if not model_id:
                    continue
                tqdm.write(f"  ↑  Uploading to Manyfold: {label}")
                ok = upload_file_to_manyfold_model(session, model_id, dest, dry_run)
            else:
                tqdm.write(f"  [dry-run] Would create model '{label}' and upload {dest.name}")
                ok = True

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

    # Manyfold
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


def main():
    parser = argparse.ArgumentParser(
        description="Sync 3D models from Bambuddy to Manyfold."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without uploading anything.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("🔍 DRY RUN mode — nothing will be uploaded.\n")

    # Validate config
    missing = []
    if BAMBUDDY_API_KEY == "YOUR_BAMBUDDY_API_KEY":
        missing.append("BAMBUDDY_API_KEY")
    if MANYFOLD_TOKEN == "YOUR_MANYFOLD_OAUTH_TOKEN":
        missing.append("MANYFOLD_TOKEN")
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
    archives_added = sync_archives(session, state, existing_names, args.dry_run)
    library_added = sync_library_files(session, state, existing_names, args.dry_run)

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
