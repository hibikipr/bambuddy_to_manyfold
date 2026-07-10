"""Unit tests for i18n.py — internationalization for bambuddy_to_manyfold.

Mirrors filament_to_bambuddy's tests/test_i18n.py: dot-key lookup, str.format()
interpolation, English fallback, resolve_locale's query/cookie/header
precedence, plus a completeness check that every language defines exactly
the same key set as English. Also covers gui_locale(), which the web-only
sibling app doesn't have (the Tkinter desktop GUI has no HTTP request to
read a language preference from, so it resolves from the OS locale instead).
"""

import i18n


class TestGetTranslation:
    def test_returns_known_key_in_requested_language(self):
        assert i18n.get_translation("de", "web.config_heading") == "Konfiguration"

    def test_returns_known_key_in_english_by_default(self):
        assert i18n.get_translation("en", "web.config_heading") == "Configuration"

    def test_unknown_language_falls_back_to_english(self):
        assert i18n.get_translation("xx", "web.config_heading") == i18n.get_translation(
            "en", "web.config_heading"
        )

    def test_interpolates_kwargs(self):
        assert i18n.get_translation("en", "log.sync_complete", elapsed="12.3") == "Sync complete in 12.3s"

    def test_interpolates_kwargs_in_non_english_language(self):
        assert i18n.get_translation("de", "log.sync_complete", elapsed="12.3") == "Synchronisierung in 12.3s abgeschlossen"

    def test_missing_interpolation_kwarg_returns_unformatted_string(self):
        result = i18n.get_translation("en", "log.sync_complete")
        assert result == "Sync complete in {elapsed}s"

    def test_missing_key_in_requested_language_falls_back_to_english(self):
        assert i18n.get_translation("de", "nonexistent.key") == "nonexistent.key"
        assert i18n.get_translation("en", "nonexistent.key") == "nonexistent.key"

    def test_key_missing_everywhere_returns_the_key(self):
        assert i18n.get_translation("en", "totally.bogus.key") == "totally.bogus.key"

    def test_non_string_leaf_returns_the_key(self):
        # "common" is a dict, not a string leaf.
        assert i18n.get_translation("en", "common") == "common"

    def test_shared_common_key_used_by_both_web_and_gui(self):
        # common.* keys exist precisely so the web app and the Tkinter GUI
        # don't maintain two copies of identical strings.
        assert i18n.get_translation("en", "common.field.bambuddy_url") == "Bambuddy URL"
        assert i18n.get_translation("de", "common.field.bambuddy_url") == "Bambuddy-URL"


class TestTranslator:
    def test_defaults_to_english(self):
        assert i18n.Translator().lang == "en"

    def test_unsupported_language_falls_back_to_english(self):
        assert i18n.Translator("xx").lang == "en"

    def test_supported_language_is_kept(self):
        assert i18n.Translator("de").lang == "de"

    def test_t_delegates_to_get_translation(self):
        assert i18n.Translator("de").t("common.field.bambuddy_url") == "Bambuddy-URL"

    def test_t_interpolates_kwargs(self):
        translator = i18n.Translator("en")
        assert translator.t("log.aborted", code=1) == "Aborted (exit code 1)"


class TestResolveLocale:
    def test_query_param_wins_over_everything(self):
        assert i18n.resolve_locale("de", query_lang="en", cookie_lang="de") == "en"

    def test_cookie_used_when_no_query_param(self):
        assert i18n.resolve_locale("en", query_lang=None, cookie_lang="de") == "de"

    def test_accept_language_used_when_no_query_or_cookie(self):
        assert i18n.resolve_locale("de-DE,de;q=0.9,en;q=0.8") == "de"

    def test_accept_language_base_subtag_matches(self):
        assert i18n.resolve_locale("de-AT,de;q=0.9") == "de"

    def test_unsupported_accept_language_falls_back_to_default(self):
        # Russian isn't one of Bambuddy's supported languages.
        assert i18n.resolve_locale("ru-RU,ru;q=0.9") == i18n.DEFAULT_LANG

    def test_no_signal_at_all_falls_back_to_default(self):
        assert i18n.resolve_locale(None) == i18n.DEFAULT_LANG

    def test_unsupported_query_param_is_ignored_falls_through_to_cookie(self):
        assert i18n.resolve_locale(None, query_lang="ru", cookie_lang="de") == "de"

    def test_query_param_is_case_insensitive(self):
        assert i18n.resolve_locale(None, query_lang="DE") == "de"

    def test_hyphenated_region_code_query_param_is_case_insensitive(self):
        assert i18n.resolve_locale(None, query_lang="ZH-cn") == "zh-cn"

    def test_accept_language_picks_first_supported_entry_in_priority_order(self):
        assert i18n.resolve_locale("ru;q=0.9,de;q=0.8") == "de"

    def test_accept_language_matches_hyphenated_region_codes_directly(self):
        assert i18n.resolve_locale("pt-BR,pt;q=0.9") == "pt-br"
        assert i18n.resolve_locale("zh-CN,zh;q=0.9") == "zh-cn"
        assert i18n.resolve_locale("zh-TW,zh;q=0.9") == "zh-tw"


class TestGuiLocale:
    """Only the Tkinter desktop GUI uses this — it has no browser/header, so
    it resolves the display language from the OS locale instead."""

    def test_explicit_override_wins(self):
        assert i18n.gui_locale("de_DE") == "de"

    def test_underscore_region_format_is_normalized(self):
        assert i18n.gui_locale("fr_FR") == "fr"

    def test_hyphenated_region_code_matches_directly(self):
        assert i18n.gui_locale("zh_CN") == "zh-cn"
        assert i18n.gui_locale("pt_BR") == "pt-br"

    def test_base_subtag_falls_back_when_region_variant_unsupported(self):
        # "de_CH" (Swiss German) has no exact-match dict, but must still
        # resolve to the base "de" translations.
        assert i18n.gui_locale("de_CH") == "de"

    def test_unsupported_locale_falls_back_to_default(self):
        assert i18n.gui_locale("ru_RU") == i18n.DEFAULT_LANG

    def test_no_override_and_no_os_locale_falls_back_to_default(self, monkeypatch):
        import locale

        monkeypatch.setattr(locale, "getlocale", lambda: (None, None))
        assert i18n.gui_locale(None) == i18n.DEFAULT_LANG

    def test_no_override_reads_os_locale(self, monkeypatch):
        import locale

        monkeypatch.setattr(locale, "getlocale", lambda: ("ja_JP", "UTF-8"))
        assert i18n.gui_locale(None) == "ja"


class TestTranslationSetsAreComplete:
    """Every language must define exactly the same set of keys as English —
    a silently-missing key in a non-English language would only be caught at
    runtime (via the fallback) instead of at test time."""

    def _flatten(self, d, prefix=""):
        keys = set()
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys |= self._flatten(v, full)
            else:
                keys.add(full)
        return keys

    def test_all_languages_have_the_same_keys_as_english(self):
        english_keys = self._flatten(i18n.EN)
        for lang, translations in i18n.TRANSLATIONS.items():
            if lang == "en":
                continue
            assert self._flatten(translations) == english_keys, f"{lang} key set mismatch"

    def test_no_empty_string_values(self):
        for lang, translations in i18n.TRANSLATIONS.items():
            for key in self._flatten(translations):
                value = translations
                for part in key.split("."):
                    value = value[part]
                assert value != "", f"{lang}.{key} is empty"
