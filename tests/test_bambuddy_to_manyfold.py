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
    _slugify,
    sync_library_files,
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
