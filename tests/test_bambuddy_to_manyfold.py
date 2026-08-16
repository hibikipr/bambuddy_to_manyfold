"""Pure-function unit tests for bambuddy_to_manyfold.py's engine helpers.

These only exercise deterministic, no-I/O logic (slugs, HTML→Markdown, tag
extraction, URL parsing, folder flattening) — no live Bambuddy/Manyfold
instance required, mirroring filament_to_bambuddy/tests/test_filament_parse.py.

The grouping-enrichment tests near the bottom are the exception: they drive
``sync_library_files`` itself (its per-file/per-group logic lives in nested
closures, not importable standalone), with every module-level function that
would otherwise touch the network mocked out.
"""

from unittest.mock import MagicMock, patch

from bambuddy_to_manyfold import (
    _extract_makerworld_tags,
    _flatten_folders,
    _html_to_markdown,
    _image_ext_from_url,
    _makerworld_model_id,
    _makerworld_profile_id,
    _model_id_from_location,
    _slugify,
    create_manyfold_model_from_upload,
    create_manyfold_model_with_files,
    import_makerworld_url,
    sync_library_files,
    sync_makerworld_urls,
)


# ── _slugify ───────────────────────────────────────────────────────────────

def test_slugify_basic():
    assert _slugify("Jane Doe") == "jane-doe"


def test_slugify_strips_punctuation():
    assert _slugify("  Foo_Bar!! Baz  ") == "foo-bar-baz"


def test_slugify_non_latin_falls_back_to_hash():
    slug = _slugify("日本語の名前")
    assert slug.startswith("creator-")
    assert len(slug) == len("creator-") + 10


def test_slugify_is_deterministic():
    assert _slugify("日本語の名前") == _slugify("日本語の名前")


# ── _html_to_markdown ──────────────────────────────────────────────────────

def test_html_to_markdown_none_input():
    assert _html_to_markdown(None) is None
    assert _html_to_markdown("") is None


def test_html_to_markdown_bold_and_paragraph():
    md = _html_to_markdown("<p>Hello <strong>world</strong></p>")
    assert "**world**" in md
    assert "Hello" in md


def test_html_to_markdown_list():
    md = _html_to_markdown("<ul><li>One</li><li>Two</li></ul>")
    assert "- One" in md
    assert "- Two" in md


def test_html_to_markdown_link():
    md = _html_to_markdown('<a href="https://example.com">click</a>')
    assert md == "[click](https://example.com)"


def test_html_to_markdown_collapses_blank_lines():
    md = _html_to_markdown("<p>A</p>\n\n\n\n<p>B</p>")
    assert "\n\n\n" not in md


# ── _extract_makerworld_tags ───────────────────────────────────────────────

def test_extract_tags_from_strings():
    assert _extract_makerworld_tags({"tags": ["Cute", " Robot ", ""]}) == ["Cute", "Robot"]


def test_extract_tags_from_dicts():
    design = {"tags": [{"name": "Toy"}, {"title": "Fun"}, {"other": "x"}]}
    assert _extract_makerworld_tags(design) == ["Toy", "Fun"]


def test_extract_tags_missing_or_wrong_type():
    assert _extract_makerworld_tags({}) == []
    assert _extract_makerworld_tags({"tags": "not-a-list"}) == []


# ── _image_ext_from_url ────────────────────────────────────────────────────

def test_image_ext_known_types():
    assert _image_ext_from_url("https://cdn.example.com/cover.jpg") == ".jpg"
    assert _image_ext_from_url("https://cdn.example.com/cover.WEBP") == ".webp"


def test_image_ext_defaults_to_png():
    assert _image_ext_from_url("https://cdn.example.com/cover") == ".png"
    assert _image_ext_from_url("https://cdn.example.com/cover.exe") == ".png"


# ── _makerworld_model_id ───────────────────────────────────────────────────

def test_makerworld_model_id_extracts_design_id():
    url = "https://makerworld.com/en/models/123456#profileId-789"
    assert _makerworld_model_id(url) == "123456"


def test_makerworld_model_id_none_input():
    assert _makerworld_model_id(None) is None


def test_makerworld_model_id_no_match():
    assert _makerworld_model_id("https://example.com/nope") is None


# ── _makerworld_profile_id ───────────────────────────────────────────────────

def test_makerworld_profile_id_extracts_id():
    url = "https://makerworld.com/en/models/123456#profileId-789"
    assert _makerworld_profile_id(url) == "789"


def test_makerworld_profile_id_legacy_equals_separator():
    assert _makerworld_profile_id("https://makerworld.com/models/1#profileId=789") == "789"


def test_makerworld_profile_id_none_input():
    assert _makerworld_profile_id(None) is None


def test_makerworld_profile_id_no_fragment():
    assert _makerworld_profile_id("https://makerworld.com/models/123456") is None


# ── _flatten_folders ───────────────────────────────────────────────────────

def test_flatten_folders_nested():
    tree = [
        {"id": 1, "name": "Root", "children": [
            {"id": 2, "name": "Child", "children": [
                {"id": 3, "name": "Grandchild", "children": []},
            ]},
        ]},
    ]
    flat = _flatten_folders(tree)
    paths = {f["id"]: f["_full_path"] for f in flat}
    assert paths == {1: "Root", 2: "Root/Child", 3: "Root/Child/Grandchild"}


def test_flatten_folders_siblings():
    tree = [
        {"id": 1, "name": "A", "children": []},
        {"id": 2, "name": "B", "children": []},
    ]
    flat = _flatten_folders(tree)
    assert [f["_full_path"] for f in flat] == ["A", "B"]


def test_flatten_folders_empty():
    assert _flatten_folders([]) == []


# ── process_group MakerWorld enrichment (grouped/multi-profile designs) ─────
#
# Regression coverage for a gap where a MakerWorld design matched to an
# ALREADY-EXISTING Manyfold model (by title, on the first run we ever see it)
# had its profile files added but never got its cover image / description /
# tags / creator applied, because that only happened on the model-creation
# branch. See process_group() in bambuddy_to_manyfold.py.

_GROUP_ENTRIES = [
    {"id": 1, "filename": "plate-a.3mf", "folder_id": None},
    {"id": 2, "filename": "plate-b.3mf", "folder_id": None},
]


def _run_group_sync(state: dict, existing_names: set):
    """Drive sync_library_files for a single 2-profile MakerWorld group,
    mocking every module-level function that would otherwise hit the network.
    Returns the mocked apply_makerworld_extras for assertions.
    """
    with (
        patch("bambuddy_to_manyfold.get_bambuddy_library_files", return_value=_GROUP_ENTRIES),
        patch(
            "bambuddy_to_manyfold.get_bambuddy_makerworld_urls",
            return_value={
                1: "https://makerworld.com/models/999#profileId-1",
                2: "https://makerworld.com/models/999#profileId-2",
            },
        ),
        patch("bambuddy_to_manyfold.get_bambuddy_library_folders", return_value=[]),
        patch("bambuddy_to_manyfold.get_existing_manyfold_collections", return_value={}),
        patch("bambuddy_to_manyfold.get_makerworld_design", return_value={"title": "My Model"}),
        patch("bambuddy_to_manyfold.download_bambuddy_library_file"),
        patch("bambuddy_to_manyfold.find_manyfold_model_id_by_name", return_value="existing123"),
        patch("bambuddy_to_manyfold.add_files_to_manyfold_model", return_value=[1, 2]),
        patch("bambuddy_to_manyfold.create_manyfold_model_with_files") as mock_create,
        patch("bambuddy_to_manyfold.apply_makerworld_extras") as mock_enrich,
    ):
        sync_library_files(
            MagicMock(), state, existing_names, dry_run=False,
        )
        mock_create.assert_not_called()  # sanity: this test is the reuse-by-name path
        return mock_enrich


def test_group_reused_by_name_still_gets_enriched():
    """First time we see this design, and Manyfold already has a same-titled
    model (e.g. created by hand) — files get added AND enrichment must run.
    """
    mock_enrich = _run_group_sync(state={}, existing_names={"My Model"})
    mock_enrich.assert_called_once()
    assert mock_enrich.call_args.args[1] == "existing123"  # model_id


def test_group_already_tracked_does_not_re_enrich():
    """A design we've already synced (and thus already enriched) in a prior
    run must NOT be re-enriched on every subsequent sync — that would
    re-attach a duplicate cover image each time.
    """
    state = {"synced_makerworld_models": {"999": "existing123"}}
    mock_enrich = _run_group_sync(state=state, existing_names={"My Model"})
    mock_enrich.assert_not_called()


# ── create_manyfold_model_from_upload / create_manyfold_model_with_files ────
#
# Regression coverage: Manyfold's single-model POST /models creation became
# synchronous (201 Created + Location header) instead of async (202
# Accepted with no way to identify the new model), but these functions only
# ever accepted 202 — every new-model creation was silently treated as a
# failure, so no model (and therefore no MakerWorld enrichment/cover image)
# was ever created for a new design.

def _mock_response(status_code, location=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Location": location} if location else {}
    resp.text = ""
    return resp


def test_model_id_from_location_absolute_url():
    resp = _mock_response(201, location="https://manyfold.example.com/models/abc123")
    assert _model_id_from_location(resp) == "abc123"


def test_model_id_from_location_relative_path():
    resp = _mock_response(201, location="/models/abc123")
    assert _model_id_from_location(resp) == "abc123"


def test_model_id_from_location_missing_header():
    assert _model_id_from_location(_mock_response(201)) is None


def test_create_model_from_upload_201_uses_location_directly():
    """Current Manyfold: synchronous creation — the model id comes straight
    from the Location header, no polling needed.
    """
    session = MagicMock()
    resp = _mock_response(201, location="https://manyfold.example.com/models/new1")
    with (
        patch("bambuddy_to_manyfold.manyfold_post", return_value=resp),
        patch("bambuddy_to_manyfold._get_all_manyfold_model_ids", return_value=set()),
        patch("bambuddy_to_manyfold._poll_for_new_manyfold_model") as mock_poll,
        patch("bambuddy_to_manyfold.add_manyfold_model_link", return_value=True) as mock_link,
        patch("bambuddy_to_manyfold.enrich_manyfold_model_from_makerworld") as mock_enrich,
    ):
        ok = create_manyfold_model_from_upload(
            session, "My Model", "https://manyfold.example.com/upload/xyz", "model.3mf",
            source_url="https://makerworld.com/models/1", source_text="MakerWorld", enrich=True,
        )
    assert ok is True
    mock_poll.assert_not_called()
    mock_link.assert_called_once_with(session, "new1", "https://makerworld.com/models/1")
    mock_enrich.assert_called_once_with(session, "new1", "My Model", "https://makerworld.com/models/1", design=None)


def test_create_model_from_upload_202_falls_back_to_polling():
    """Older Manyfold / genuinely async creation: 202 with no Location still
    works via the existing poll-by-diff fallback.
    """
    session = MagicMock()
    resp = _mock_response(202)
    with (
        patch("bambuddy_to_manyfold.manyfold_post", return_value=resp),
        patch("bambuddy_to_manyfold._get_all_manyfold_model_ids", return_value={"old1"}),
        patch("bambuddy_to_manyfold._poll_for_new_manyfold_model", return_value="new2") as mock_poll,
        patch("bambuddy_to_manyfold.add_manyfold_model_link", return_value=True) as mock_link,
        patch("bambuddy_to_manyfold.enrich_manyfold_model_from_makerworld") as mock_enrich,
    ):
        ok = create_manyfold_model_from_upload(
            session, "My Model", "https://manyfold.example.com/upload/xyz", "model.3mf",
            source_url="https://makerworld.com/models/1", source_text="MakerWorld", enrich=True,
        )
    assert ok is True
    mock_poll.assert_called_once()
    mock_link.assert_called_once_with(session, "new2", "https://makerworld.com/models/1")
    mock_enrich.assert_called_once()


def test_create_model_from_upload_rejects_unexpected_status():
    resp = _mock_response(500)
    with patch("bambuddy_to_manyfold.manyfold_post", return_value=resp):
        ok = create_manyfold_model_from_upload(
            MagicMock(), "My Model", "https://manyfold.example.com/upload/xyz", "model.3mf",
        )
    assert ok is False


def test_create_model_with_files_201_uses_location_directly():
    session = MagicMock()
    resp = _mock_response(201, location="https://manyfold.example.com/models/grp1")
    with (
        patch("bambuddy_to_manyfold._tus_upload_many", return_value=([{"id": "u1", "name": "a.3mf"}], [1, 2])),
        patch("bambuddy_to_manyfold._get_all_manyfold_model_ids", return_value=set()),
        patch("bambuddy_to_manyfold.manyfold_post", return_value=resp),
        patch("bambuddy_to_manyfold._poll_for_new_manyfold_model") as mock_poll,
    ):
        model_id, ok_ids = create_manyfold_model_with_files(
            session, "Grouped Model", [(1, "a.3mf"), (2, "b.3mf")], None,
        )
    assert model_id == "grp1"
    assert ok_ids == [1, 2]
    mock_poll.assert_not_called()


def test_create_model_with_files_202_falls_back_to_polling():
    session = MagicMock()
    resp = _mock_response(202)
    with (
        patch("bambuddy_to_manyfold._tus_upload_many", return_value=([{"id": "u1", "name": "a.3mf"}], [1, 2])),
        patch("bambuddy_to_manyfold._get_all_manyfold_model_ids", return_value={"old1"}),
        patch("bambuddy_to_manyfold.manyfold_post", return_value=resp),
        patch("bambuddy_to_manyfold._poll_for_new_manyfold_model", return_value="grp2") as mock_poll,
    ):
        model_id, ok_ids = create_manyfold_model_with_files(
            session, "Grouped Model", [(1, "a.3mf"), (2, "b.3mf")], None,
        )
    assert model_id == "grp2"
    assert ok_ids == [1, 2]
    mock_poll.assert_called_once()


# ── process_single MakerWorld enrichment (single-profile designs) ───────────
#
# Same gap as process_group, but in the singles path: a design whose model
# already exists in Manyfold by title match was fully skipped — no file
# added, no enrichment attempted — and crucially this skip ignored --force,
# so even an explicit forced resync could never pick up a missing cover
# image once the model existed.

_SINGLE_ENTRY = [{"id": 1, "filename": "solo.3mf", "folder_id": None}]


def _run_single_sync(state: dict, existing_names: set, force: bool = False):
    """Drive sync_library_files for a single non-grouped MakerWorld file,
    mocking every module-level function that would otherwise hit the network.
    Returns the mocked apply_makerworld_extras for assertions.
    """
    with (
        patch("bambuddy_to_manyfold.get_bambuddy_library_files", return_value=_SINGLE_ENTRY),
        patch(
            "bambuddy_to_manyfold.get_bambuddy_makerworld_urls",
            return_value={1: "https://makerworld.com/models/555#profileId-1"},
        ),
        patch("bambuddy_to_manyfold.get_bambuddy_library_folders", return_value=[]),
        patch("bambuddy_to_manyfold.get_existing_manyfold_collections", return_value={}),
        patch("bambuddy_to_manyfold.get_makerworld_design", return_value={"title": "Solo Model"}),
        patch("bambuddy_to_manyfold.find_manyfold_model_id_by_name", return_value="existing456"),
        patch("bambuddy_to_manyfold.apply_makerworld_extras") as mock_enrich,
        patch("bambuddy_to_manyfold.upload_model_to_manyfold") as mock_upload,
    ):
        sync_library_files(MagicMock(), state, existing_names, dry_run=False, force=force)
        mock_upload.assert_not_called()  # sanity: this test is the reuse-by-name path
        return mock_enrich


def test_single_reused_by_name_still_gets_enriched():
    """First time we see this design, and Manyfold already has a same-titled
    model — enrichment must run even though no new file is uploaded.
    """
    mock_enrich = _run_single_sync(state={}, existing_names={"Solo Model"})
    mock_enrich.assert_called_once()
    assert mock_enrich.call_args.args[1] == "existing456"  # model_id


def test_single_already_synced_does_not_re_enrich_without_force():
    """A file we've already processed in a prior run must NOT be re-enriched
    on every subsequent ordinary sync.
    """
    state = {"synced_library_files": [1]}
    mock_enrich = _run_single_sync(state=state, existing_names={"Solo Model"})
    mock_enrich.assert_not_called()


def test_single_force_re_enriches_already_synced():
    """--force is the user's explicit request to redo the work — it must
    actually re-attempt enrichment even for a file already marked synced.
    """
    state = {"synced_library_files": [1]}
    mock_enrich = _run_single_sync(state=state, existing_names={"Solo Model"}, force=True)
    mock_enrich.assert_called_once()


# ── import_makerworld_url ────────────────────────────────────────────────────

def _mock_import_response(status_ok, library_file_id=None, profile_id=None, status_code=200, text=""):
    resp = MagicMock()
    resp.ok = status_ok
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = {
        "library_file_id": library_file_id,
        "filename": "solo.3mf",
        "folder_id": None,
        "profile_id": profile_id,
        "was_existing": False,
    }
    return resp


def test_import_makerworld_url_rejects_non_makerworld_url():
    session = MagicMock()
    file_id, canonical_url, error = import_makerworld_url(session, "https://example.com/nope")
    assert file_id is None
    assert canonical_url is None
    assert "Not a recognisable MakerWorld" in error
    session.post.assert_not_called()


def test_import_makerworld_url_success_with_explicit_profile():
    session = MagicMock()
    session.post.return_value = _mock_import_response(True, library_file_id=42, profile_id=789)
    file_id, canonical_url, error = import_makerworld_url(
        session, "https://makerworld.com/models/123#profileId-789",
    )
    assert error is None
    assert file_id == 42
    assert canonical_url == "https://makerworld.com/models/123#profileId-789"
    assert session.post.call_args.kwargs["json"] == {"model_id": 123, "profile_id": 789}


def test_import_makerworld_url_bare_url_uses_resolved_profile():
    """A bare model URL (no #profileId-) still ends up with a specific
    profile in the canonical URL — Bambuddy picks a default and reports it.
    """
    session = MagicMock()
    session.post.return_value = _mock_import_response(True, library_file_id=42, profile_id=555)
    file_id, canonical_url, error = import_makerworld_url(session, "https://makerworld.com/models/123")
    assert error is None
    assert canonical_url == "https://makerworld.com/models/123#profileId-555"
    assert session.post.call_args.kwargs["json"] == {"model_id": 123}  # no profile_id sent — let Bambuddy pick


def test_import_makerworld_url_http_failure():
    session = MagicMock()
    session.post.return_value = _mock_import_response(False, status_code=502, text="bad gateway")
    file_id, canonical_url, error = import_makerworld_url(session, "https://makerworld.com/models/123")
    assert file_id is None
    assert canonical_url is None
    assert "502" in error


def test_import_makerworld_url_missing_library_file_id():
    session = MagicMock()
    session.post.return_value = _mock_import_response(True, library_file_id=None)
    file_id, canonical_url, error = import_makerworld_url(session, "https://makerworld.com/models/123")
    assert file_id is None
    assert "no library_file_id" in error


def test_import_makerworld_url_request_exception():
    session = MagicMock()
    session.post.side_effect = RuntimeError("connection reset")
    file_id, canonical_url, error = import_makerworld_url(session, "https://makerworld.com/models/123")
    assert file_id is None
    assert "connection reset" in error


# ── sync_makerworld_urls ─────────────────────────────────────────────────────

def test_sync_makerworld_urls_dedupes_and_syncs():
    urls = [
        "https://makerworld.com/models/1#profileId-11",
        "https://makerworld.com/en/models/1#profileId-11",  # dup, different path prefix
        "https://makerworld.com/models/2",
    ]
    with (
        patch(
            "bambuddy_to_manyfold.import_makerworld_url",
            side_effect=[
                (101, "https://makerworld.com/models/1#profileId-11", None),
                (102, "https://makerworld.com/models/2#profileId-99", None),
            ],
        ) as mock_import,
        patch("bambuddy_to_manyfold.sync_library_files", return_value=2) as mock_sync,
    ):
        count = sync_makerworld_urls(MagicMock(), {}, set(), urls, dry_run=False)
    assert mock_import.call_count == 2  # the duplicate never triggered a second import
    assert count == 2
    kwargs = mock_sync.call_args.kwargs
    assert kwargs["selected_ids"] == {101, 102}
    assert kwargs["extra_makerworld_urls"] == {
        101: "https://makerworld.com/models/1#profileId-11",
        102: "https://makerworld.com/models/2#profileId-99",
    }


def test_sync_makerworld_urls_skips_invalid_lines_but_processes_rest():
    urls = ["not a url", "https://makerworld.com/models/1"]
    with (
        patch(
            "bambuddy_to_manyfold.import_makerworld_url",
            return_value=(101, "https://makerworld.com/models/1#profileId-11", None),
        ) as mock_import,
        patch("bambuddy_to_manyfold.sync_library_files", return_value=1),
    ):
        count = sync_makerworld_urls(MagicMock(), {}, set(), urls, dry_run=False)
    mock_import.assert_called_once()
    assert count == 1


def test_sync_makerworld_urls_dry_run_makes_no_import_calls():
    urls = ["https://makerworld.com/models/1", "https://makerworld.com/models/2"]
    with (
        patch("bambuddy_to_manyfold.import_makerworld_url") as mock_import,
        patch("bambuddy_to_manyfold.sync_library_files") as mock_sync,
    ):
        count = sync_makerworld_urls(MagicMock(), {}, set(), urls, dry_run=True)
    mock_import.assert_not_called()
    mock_sync.assert_not_called()
    assert count == 2


def test_sync_makerworld_urls_partial_import_failure_still_syncs_successes():
    urls = ["https://makerworld.com/models/1", "https://makerworld.com/models/2"]
    with (
        patch(
            "bambuddy_to_manyfold.import_makerworld_url",
            side_effect=[
                (None, None, "MakerWorld import failed for .../1: 403 forbidden"),
                (102, "https://makerworld.com/models/2#profileId-99", None),
            ],
        ),
        patch("bambuddy_to_manyfold.sync_library_files", return_value=1) as mock_sync,
    ):
        count = sync_makerworld_urls(MagicMock(), {}, set(), urls, dry_run=False)
    assert count == 1
    assert mock_sync.call_args.kwargs["selected_ids"] == {102}


def test_sync_makerworld_urls_all_imports_fail_skips_sync():
    with (
        patch("bambuddy_to_manyfold.import_makerworld_url", return_value=(None, None, "boom")),
        patch("bambuddy_to_manyfold.sync_library_files") as mock_sync,
    ):
        count = sync_makerworld_urls(MagicMock(), {}, set(), ["https://makerworld.com/models/1"], dry_run=False)
    assert count == 0
    mock_sync.assert_not_called()


def test_sync_makerworld_urls_no_valid_urls_skips_everything():
    with (
        patch("bambuddy_to_manyfold.import_makerworld_url") as mock_import,
        patch("bambuddy_to_manyfold.sync_library_files") as mock_sync,
    ):
        count = sync_makerworld_urls(MagicMock(), {}, set(), ["garbage", ""], dry_run=False)
    assert count == 0
    mock_import.assert_not_called()
    mock_sync.assert_not_called()
