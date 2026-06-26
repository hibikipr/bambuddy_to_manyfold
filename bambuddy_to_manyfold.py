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
    return {"synced_archives": [], "synced_library_files": []}


def save_sync_state(state: dict):
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Bambuddy helpers ──────────────────────────────────────────────────────────

def bambuddy_headers() -> dict:
    return {"X-API-Key": BAMBUDDY_API_KEY, "Accept": "application/json"}


def get_bambuddy_archives(session: requests.Session) -> list[dict]:
    """Fetch all print archives (completed 3MF prints) from Bambuddy."""
    archives = []
    page = 1
    print("  Fetching Bambuddy archives...")
    while True:
        resp = session.get(
            f"{BAMBUDDY_URL}/api/v1/archives",
            params={"page": page, "per_page": PAGE_SIZE, "status": "success"},
            headers=bambuddy_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("archives", [])
        archives.extend(batch)
        print(f"    Page {page}: {len(batch)} archives (total so far: {len(archives)})")
        if len(archives) >= data.get("total", 0) or not batch:
            break
        page += 1
    return archives


def get_bambuddy_library_files(session: requests.Session) -> list[dict]:
    """Fetch all files from the Bambuddy file manager library."""
    files = []
    page = 1
    print("  Fetching Bambuddy library files...")
    while True:
        resp = session.get(
            f"{BAMBUDDY_URL}/api/v1/library",
            params={"page": page, "per_page": PAGE_SIZE},
            headers=bambuddy_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # The library endpoint may return a list or a paginated dict; handle both
        if isinstance(data, list):
            batch = data
        else:
            batch = data.get("files", data.get("items", []))
        files.extend(batch)
        print(f"    Page {page}: {len(batch)} files (total so far: {len(files)})")
        total = data.get("total", len(files)) if isinstance(data, dict) else len(files)
        if len(files) >= total or not batch:
            break
        page += 1
    return files


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
        f"{BAMBUDDY_URL}/api/v1/library/{file_id}/download",
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
        "Accept": "application/vnd.api+json",
    }


def get_existing_manyfold_models(session: requests.Session) -> set[str]:
    """
    Return a set of model names already in the target Manyfold library.
    Used to skip duplicates.
    """
    names = set()
    page = 1
    print("  Fetching existing Manyfold models...")
    while True:
        resp = session.get(
            f"{MANYFOLD_URL}/api/v1/models",
            params={"page[number]": page, "page[size]": 100,
                    "filter[library_id]": MANYFOLD_LIBRARY_ID},
            headers=manyfold_headers(),
            timeout=30,
        )
        if resp.status_code == 404:
            # Older Manyfold without library filter — fetch all
            resp = session.get(
                f"{MANYFOLD_URL}/api/v1/models",
                params={"page[number]": page, "page[size]": 100},
                headers=manyfold_headers(),
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        for m in models:
            name = m.get("attributes", {}).get("name", "")
            if name:
                names.add(name)
        meta = data.get("meta", {})
        total_pages = meta.get("total_pages", 1)
        print(f"    Page {page}/{total_pages}: {len(models)} models")
        if page >= total_pages or not models:
            break
        page += 1
    return names


def create_manyfold_model(session: requests.Session, name: str) -> str | None:
    """Create an empty model in Manyfold and return its ID."""
    payload = {
        "data": {
            "type": "model",
            "attributes": {"name": name},
            "relationships": {
                "library": {
                    "data": {"type": "library", "id": str(MANYFOLD_LIBRARY_ID)}
                }
            },
        }
    }
    resp = session.post(
        f"{MANYFOLD_URL}/api/v1/models",
        json=payload,
        headers={**manyfold_headers(), "Content-Type": "application/vnd.api+json"},
        timeout=30,
    )
    if not resp.ok:
        print(f"    ⚠️  Failed to create model '{name}': {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json()["data"]["id"]


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
            f"{MANYFOLD_URL}/api/v1/model_files",
            headers={
                "Authorization": f"Bearer {MANYFOLD_TOKEN}",
                "Accept": "application/vnd.api+json",
            },
            files={"file": (file_path.name, f)},
            data={
                "model_file[model_id]": model_id,
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
    existing_names: set[str],
    dry_run: bool,
) -> int:
    archives = get_bambuddy_archives(session)
    synced_ids: set = set(state["synced_archives"])
    new_count = 0

    print(f"\n📦 Syncing {len(archives)} Bambuddy archives...")
    for archive in tqdm(archives, unit="archive"):
        archive_id = archive.get("id")
        name = archive.get("name") or archive.get("filename", f"archive_{archive_id}")
        # Strip extension for the model name
        model_name = Path(name).stem

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
    existing_names: set[str],
    dry_run: bool,
) -> int:
    lib_files = get_bambuddy_library_files(session)
    synced_ids: set = set(state["synced_library_files"])
    new_count = 0

    # Filter to supported extensions
    supported = [
        f for f in lib_files
        if Path(f.get("filename", f.get("name", ""))).suffix.lower() in LIBRARY_EXTENSIONS
    ]
    print(f"\n📁 Syncing {len(supported)}/{len(lib_files)} Bambuddy library files (filtered by extension)...")

    for file_entry in tqdm(supported, unit="file"):
        file_id = file_entry.get("id")
        filename = file_entry.get("filename") or file_entry.get("name", f"file_{file_id}")
        model_name = Path(filename).stem

        if file_id in synced_ids:
            tqdm.write(f"  ⏭  Already synced: {model_name}")
            continue

        if model_name in existing_names:
            tqdm.write(f"  ⏭  Already in Manyfold (skipping duplicate): {model_name}")
            synced_ids.add(file_id)
            continue

        tqdm.write(f"  ↓  Downloading: {filename}")
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / filename
            try:
                download_bambuddy_library_file(session, file_id, dest)
            except Exception as e:
                tqdm.write(f"  ⚠️  Download failed for library file {file_id}: {e}")
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
            synced_ids.add(file_id)
            existing_names.add(model_name)
            new_count += 1

    state["synced_library_files"] = list(synced_ids)
    return new_count


def check_connections(session: requests.Session):
    """Quick sanity-check that both services are reachable."""
    print("🔌 Checking connections...")

    # Bambuddy
    try:
        r = session.get(
            f"{BAMBUDDY_URL}/api/v1/printers",
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
            f"{MANYFOLD_URL}/api/v1/models",
            params={"page[size]": 1},
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
