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

## Requirements

```bash
pip install requests tqdm
```

`tkinter` is needed for the GUI. It ships with Python on macOS and Windows; on
Linux install it separately, e.g. `sudo apt install python3-tk`.

---

## Manyfold authentication (important)

Uploading files requires the **`upload`** OAuth scope. Personal access tokens in
Manyfold **cannot** carry the `upload` scope — you **must** use the OAuth
**client_credentials** flow.

1. In Manyfold, go to **Settings → API** and create an OAuth application.
2. Grant it the scopes: **`public read write upload`**.
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
```

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Logs what would be synced. No uploads, no state-file writes. |
| `--no-create` | Skips any model not already present in Manyfold. |
| `--force` | Ignores the local sync-state file so already-recorded items are re-processed. The live "already in Manyfold" check still applies, so this won't create duplicates of models that genuinely synced. |
| `--no-links` | Skips attaching MakerWorld source URLs as links on synced models. |

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
3. In the two lists, tick the models you want. Each section has **All** / **None**
   buttons, and you can click any row to toggle it.
4. Click **▶ Run sync**.

### Options

- **Dry run (no uploads)** — preview only; nothing is written.
- **Create missing models in Manyfold** — when unticked, only uploads to models
  that already exist (equivalent to `--no-create`).
- **Force re-sync (ignore sync state)** — re-process selected items even if the
  state file lists them as synced (equivalent to `--force`).
- **Add MakerWorld links** — attach the MakerWorld source URL (for library files
  imported from MakerWorld) as a clickable link on the created Manyfold model.
- **Log debug** — show verbose diagnostics (pagination, scope probe, etc.).

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
  file IDs, and a Bambuddy-folder → Manyfold-collection mapping.
- **MakerWorld links** — files imported into Bambuddy via "Import from MakerWorld"
  carry a source URL. The sync fetches these from `GET /makerworld/recent-imports`
  and, after a model is created, PATCHes the URL on as a Manyfold link. Because
  that endpoint is capped at 50 rows, only the 50 most recent MakerWorld imports
  get links; the upload endpoint itself can't accept links, so this is done as a
  best-effort follow-up (a file still syncs if the link step can't find it).

---

## Notes & gotchas

- Manyfold creates models from uploaded files in a **background job**. A `202`
  response means *accepted*, not *done* — the model appears a few seconds later.
  If a job fails server-side, the item may be recorded as synced locally even
  though it didn't land; use `--force` (or the GUI checkbox) to re-push it.
- The script always sends `isPartOf` (an empty array when there's no collection).
  Omitting it triggers a `nil` error in Manyfold's upload job.
- Supported library extensions: `.3mf`, `.stl`, `.obj`, `.step`, `.stp`.

---

🤖 Built with [Claude Code](https://claude.com/claude-code)
