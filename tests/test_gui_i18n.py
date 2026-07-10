"""Tests for i18n wiring in bambuddy_to_manyfold_gui.py.

Deliberately import-only, no tk.Tk()/App() instantiation: this repo's CI
(ubuntu-latest, no Xvfb) has no display, so any test that actually builds a
Tkinter window would fail there even though it works fine locally (verified
manually against both the default English and a simulated Japanese OS
locale during development). What's covered instead is the piece that's
pure Python and CI-safe: the module resolves its language once at import
time from the OS locale (i18n.gui_locale()) and exposes a bound `t`
function that every widget-construction call site in App._build_ui() and
the worker methods uses.
"""

import importlib

import bambuddy_to_manyfold_gui as gui
import i18n


class TestModuleLevelTranslatorWiring:
    def test_t_is_bound_to_a_translator(self):
        assert callable(gui.t)
        assert isinstance(gui._translator, i18n.Translator)

    def test_t_resolves_known_keys(self):
        assert gui.t("common.title") == "Bambuddy → Manyfold Sync"
        assert gui.t("gui.load_models_button") == "⟳  Load models"

    def test_t_interpolates_kwargs(self):
        assert gui.t("gui.archives_tab_counted", total=3, synced=1) == "Archives  (3 · 1 synced)"

    def test_module_language_follows_os_locale_at_import_time(self, monkeypatch):
        import locale

        monkeypatch.setattr(locale, "getlocale", lambda: ("de_DE", "UTF-8"))
        reloaded = importlib.reload(gui)
        try:
            assert reloaded._translator.lang == "de"
            assert reloaded.t("gui.load_models_button") == "⟳  Modelle laden"
        finally:
            # Restore the real OS locale and reload again so later test
            # modules (and a second run of this one) see the normal module.
            monkeypatch.undo()
            importlib.reload(gui)

    def test_sort_label_to_key_source_strings_exist_in_every_language(self):
        # App._build_ui() builds _SORT_LABEL_TO_KEY from these three exact
        # keys — if a translation ever went missing, sorting would silently
        # break in that language. Can't instantiate App() here (no display
        # in CI), so assert the underlying keys directly instead.
        for lang in i18n.SUPPORTED_LANGS:
            translator = i18n.Translator(lang)
            for key in ("common.action.name", "common.action.date", "common.action.status"):
                value = translator.t(key)
                assert value != key, f"{lang}.{key} missing translation"
