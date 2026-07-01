"""Pure-function unit tests for bambuddy_to_manyfold.py's engine helpers.

These only exercise deterministic, no-I/O logic (slugs, HTML→Markdown, tag
extraction, URL parsing, folder flattening) — no live Bambuddy/Manyfold
instance required, mirroring filament_to_bambuddy/tests/test_filament_parse.py.
"""

from bambuddy_to_manyfold import (
    _extract_makerworld_tags,
    _flatten_folders,
    _html_to_markdown,
    _image_ext_from_url,
    _makerworld_model_id,
    _slugify,
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
