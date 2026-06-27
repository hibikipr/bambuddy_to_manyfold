# bambuddy_to_manyfold

Sync 3D models from a [Bambuddy](https://github.com/) instance to [Manyfold](https://manyfold.app).

It syncs both:

- **Print archives** — completed prints from the Bambuddy archive.
- **File manager library** — every file in the Bambuddy file manager, recreating
  the folder hierarchy as nested **Manyfold collections**.

Each model is uploaded to Manyfold via its resumable (Tus) upload endpoint and
turned into a model. A local state file tracks what's already been synced so
re-runs only upload new items.

---

## Tested with

- **Bambuddy** v0.2.4.7
- **Manyfold** v0.142.0 (`dbd95979`)

Other versions may work but aren't verified — the script depends on Manyfold's
v0 API (model/file Tus uploads, collections, creators) and Bambuddy's MakerWorld
+ library endpoints, which can change between releases.

---

## Requirements

**Python 3.10 or newer** is required (the code uses `X | None` type hints, which
older versions reject at import time). Both scripts check this on startup and
exit with a clear message if run on an older interpreter.

```bash
pip install requests tqdm
```

`tkinter` is needed for the GUI. It ships with Python on macOS and Windows; on
Linux install it separately, e.g. `sudo apt install python3-tk`.

### macOS: watch out for the wrong `python3`

macOS bundles an old Python at `/usr/bin/python3` (currently **3.9**), which is
**too old** — running the scripts with it fails. Install a current build from
[python.org](https://www.python.org/downloads/) (or Homebrew) and invoke it
explicitly to be safe:

```bash
python3.14 bambuddy_to_manyfold_gui.py      # GUI
python3.14 bambuddy_to_manyfold.py --dry-run # CLI
```

Check what your `python3` resolves to with `python3 --version`. The included
[`run_sync.sh`](run_sync.sh) auto-selects a Python 3.10+ interpreter for you and
prints which one it used.

---

## Manyfold authentication (important)

Uploading files requires the **`upload`** OAuth scope. Personal access tokens in
Manyfold **cannot** carry the `upload` scope — you **must** use the OAuth
**client_credentials** flow.

1. In Manyfold, go to **Settings → API** and create an OAuth application.
2. Grant it the scopes: **`public read write upload`** (add **`delete`** too if you
   want to use the empty-model cleanup — see `--cleanup-empty`).
3. Note its **Client ID** and **Client Secret**.

The script exchanges these for a short-lived token automatically and requests the
`upload` scope. On startup it prints the granted scopes and runs a quick probe to
confirm `upload` is present before doing any work — if it's missing it stops with
instructions rather than failing halfway.

> A pre-issued `MANYFOLD_TOKEN` is still accepted as a fallback for read/list
> operations, but uploads will fail without `upload` scope. Use client
> credentials for syncing.

---

## Configuration

All settings come from environment variables (or the defaults baked into the
script). The GUI exposes the same settings as form fields and remembers them
between runs.

| Variable | Required | Description |
|---|---|---|
| `BAMBUDDY_URL` | yes | Base URL of your Bambuddy instance |
| `BAMBUDDY_API_KEY` | yes | Bambuddy API key |
| `MANYFOLD_URL` | yes | Base URL of your Manyfold instance |
| `MANYFOLD_CLIENT_ID` | yes* | OAuth application client ID (client_credentials flow) |
| `MANYFOLD_CLIENT_SECRET` | yes* | OAuth application client secret |
| `MANYFOLD_TOKEN` | — | Pre-issued token (fallback; cannot upload) |
| `MANYFOLD_SCOPES` | — | Scopes to request (default `public read write upload`) |
| `MANYFOLD_LIBRARY_ID` | — | Manyfold library to upload into (default `1`) |
| `SYNC_STATE_FILE` | — | Path to the sync-state JSON (default `bambuddy_sync_state.json`) |
| `MANYFOLD_SYNC_DEBUG` | — | `1` to enable verbose diagnostic output |

\* Either `MANYFOLD_CLIENT_ID` + `MANYFOLD_CLIENT_SECRET`, **or** a
`MANYFOLD_TOKEN`, must be set. Use client credentials to upload.

---

## Command-line usage

```bash
# Sync everything not already synced
python3 bambuddy_to_manyfold.py

# See what would happen without uploading anything
python3 bambuddy_to_manyfold.py --dry-run

# Only add files to models that already exist in Manyfold; never create new ones
python3 bambuddy_to_manyfold.py --no-create

# Ignore the local sync-state file and re-process items
# (recovers uploads whose background job failed)
python3 bambuddy_to_manyfold.py --force

# Don't attach MakerWorld source URLs as links on synced models
python3 bambuddy_to_manyfold.py --no-links

# Don't fetch MakerWorld details (description, tags, cover image)
python3 bambuddy_to_manyfold.py --no-enrich

# Don't group multiple MakerWorld profiles of one design into a single model
python3 bambuddy_to_manyfold.py --no-group

# Clean up: delete Manyfold models that have no files (e.g. failed uploads).
# Defaults to the "MakerWorld" collection; pass a name, or ALL for everything.
# Combine with --dry-run to preview. Requires the 'delete' OAuth scope.
python3 bambuddy_to_manyfold.py --cleanup-empty --dry-run
python3 bambuddy_to_manyfold.py --cleanup-empty "MakerWorld"
python3 bambuddy_to_manyfold.py --cleanup-empty ALL
```

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Logs what would be synced. No uploads, no state-file writes. |
| `--no-create` | Skips any model not already present in Manyfold. |
| `--force` | Ignores the local sync-state file so already-recorded items are re-processed. The live "already in Manyfold" check still applies, so this won't create duplicates of models that genuinely synced. |
| `--no-links` | Skips attaching MakerWorld source URLs as links on synced models. |
| `--no-enrich` | Skips fetching MakerWorld details (description, tags, cover image). |
| `--no-group` | Syncs each MakerWorld profile as its own model instead of grouping profiles of the same design into one. |
| `--cleanup-empty [COLLECTION]` | Instead of syncing, deletes Manyfold models that have **no files** (e.g. left over from failed uploads). Limits to a collection by name (default `MakerWorld`), or pass `ALL` for every collection. Respects `--dry-run`. Needs the **`delete`** OAuth scope (see below). |

A convenience wrapper, [`run_sync.sh`](run_sync.sh), exports the env vars and
runs the script. Edit it with your values, then `./run_sync.sh [--dry-run]`.

---

## GUI usage

```bash
python3 bambuddy_to_manyfold_gui.py
```

Workflow:

1. Fill in the **Configuration** fields (client ID/secret are masked, with a
   **Show** toggle). Settings persist to `~/.bambuddy_to_manyfold_gui.json`.
2. Click **⟳ Load models** to fetch archives and library files from Bambuddy.
   Items already in the sync state show greyed out and pre-unchecked.
3. Pick models in the **Archives** and **Library files** tabs (each tab shows its
   item / synced counts). Tick the ones you want — **Select all** / **Select none**
   per tab, or click any row to toggle it. The shared **Sort by** (Name / Date /
   Status) control + **Descending** and **Hide already-synced** apply to both
   tabs; you can also click a column header to sort.
4. Click **▶ Run sync** — the **Output** pane below stays visible so you can watch
   progress. When it finishes, the lists reload automatically so statuses update
   (newly-synced items show as `synced`).

### Options

- **Dry run (no uploads)** — preview only; nothing is written.
- **Create missing models in Manyfold** — when unticked, only uploads to models
  that already exist (equivalent to `--no-create`).
- **Force re-sync (ignore sync state)** — re-process selected items even if the
  state file lists them as synced (equivalent to `--force`).
- **Add MakerWorld links** — attach the MakerWorld source URL (for library files
  imported from MakerWorld) as a clickable link on the created Manyfold model.
- **Fetch MakerWorld details (description + cover)** — pull the model's name,
  description, tags, creator, and cover image from MakerWorld and apply them to
  the Manyfold model (the cover is also set as the model's preview image).
- **Group MakerWorld profiles into one model** — Bambuddy stores each imported
  MakerWorld profile (plate) as a separate file. With this on, profiles of the
  same design are combined into a single Manyfold model holding all the plate
  files, instead of one model per profile.
- **Log debug** — show verbose diagnostics (pagination, scope probe, etc.).

The **🧹 Clean empty models** button deletes Manyfold models that have no files
(handy for clearing failed uploads). It asks for a collection name (default
`MakerWorld`, or `ALL`), confirms before deleting, and respects the **Dry run**
checkbox so you can preview first. Needs the `delete` OAuth scope.

Output streams live into the log pane, colour-coded, with a timestamped marker
at the start of each load/sync.

---

## How it works

- **Archives** → `GET /api/v1/archives/` → downloaded → uploaded to Manyfold as a
  model (no collection).
- **Library files** → `GET /api/v1/library/files?include_root=false` (all folders)
  plus `GET /api/v1/library/folders` for the tree. Each Bambuddy folder becomes a
  Manyfold collection (nested via `isPartOf`), and files are uploaded into the
  matching collection.
- **Upload** is a two-step Manyfold flow: the file is sent to the Tus endpoint
  (`/upload`, resumable, chunked), then `POST /models` references the upload to
  create the model asynchronously (returns `202 Accepted`).
- **State** is tracked in the sync-state JSON: synced archive IDs, synced library
  file IDs, a Bambuddy-folder → Manyfold-collection mapping, and a MakerWorld
  design-id → Manyfold-model-id mapping (so later profile imports of a design are
  added to its existing grouped model).
- **Grouping** keys on the MakerWorld design id (the number in `/models/{id}`,
  shared by every profile of a design). The first profile creates one Manyfold
  model — named/enriched from the design — and the remaining profiles are added
  to it as files. Profiles imported in a later run attach to the same model via
  the stored design-id mapping.
- **MakerWorld links + details** — files imported into Bambuddy via "Import from
  MakerWorld" carry a source URL. The sync fetches these from
  `GET /makerworld/recent-imports` and, after a model is created, PATCHes the URL
  on as a Manyfold link. When **enrichment** is enabled it also resolves the
  MakerWorld design (via Bambuddy's `POST /makerworld/resolve`) and applies to
  the Manyfold model:
    - **name** ← the MakerWorld design title (resolved *before* creation, so the
      model is named — and de-duplicated — by its title from the start)
    - **description** (notes) ← the design summary, converted from HTML to Markdown
    - **tags** (keywords) ← the design tags
    - **creator** ← the MakerWorld designer (found or created in Manyfold by name)
    - **cover image** ← uploaded as a model file and set as the model's preview

  Because `recent-imports` is capped at 50 rows, only the 50 most recent
  MakerWorld imports get links/details; the upload endpoint can't accept links,
  metadata or images directly, so all of this is a best-effort follow-up after
  the async model-creation job (a file still syncs if the model can't be located
  in time).

---

## Notes & gotchas

- Manyfold creates models from uploaded files in a **background job**. A `202`
  response means *accepted*, not *done* — the model appears a few seconds later.
  If a job fails server-side, the item may be recorded as synced locally even
  though it didn't land; use `--force` (or the GUI checkbox) to re-push it.
- The script always sends `isPartOf` (an empty array when there's no collection).
  Omitting it triggers a `nil` error in Manyfold's upload job.
- **Rate limiting:** Manyfold caps model creation and file-adding at **10 per 3
  minutes** each, returning `429`. The script waits and retries (long enough to
  ride out the full 3-minute window). To minimise how often it hits the limit,
  all files of a grouped MakerWorld design are sent in a **single request** (one
  model with many files, rather than one request per file). Even so, a large
  first-time sync of many *separate* models is slow by design (~10 new models per
  3 minutes). Anything that still can't be added is left unsynced and retried on
  the next run — nothing is silently lost.
- Supported library extensions: `.3mf`, `.stl`, `.obj`, `.step`, `.stp`.

---

🤖 Built with [Claude Code](https://claude.com/claude-code)
