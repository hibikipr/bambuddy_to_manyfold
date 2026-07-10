"""Integration tests for i18n wiring in bambuddy_manyfold_web.py.

Covers the full precedence chain (query > cookie > Accept-Language >
default) end-to-end through a real Flask test client on the `/` route, the
embedded window.I18N blob, translated validation-error JSON responses on
the job-control endpoints, and that _banner()/SyncJob correctly bind a
per-job Translator — the piece this app has that the sibling
filament_to_bambuddy app doesn't (a background job whose print()-based log
is shared, language-fixed-at-start-time, state).
"""

import pytest

import bambuddy_manyfold_web as web
import i18n


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate config file + sync state so tests never touch a real config
    # or need real Bambuddy/Manyfold credentials.
    monkeypatch.setattr(web, "WEB_CONFIG_FILE", tmp_path / "web_config.json")
    web.app.testing = True
    return web.app.test_client()


class TestIndexRouteLanguage:
    def test_defaults_to_english_with_no_signal(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b'<html lang="en">' in resp.data
        assert "Configuration".encode() in resp.data

    def test_query_param_overrides_default(self, client):
        resp = client.get("/?lang=de")
        assert resp.status_code == 200
        assert b'<html lang="de">' in resp.data
        assert "Konfiguration".encode() in resp.data

    def test_query_param_sets_a_persistent_cookie(self, client):
        resp = client.get("/?lang=de")
        assert "lang=de" in resp.headers.get("Set-Cookie", "")

    def test_unsupported_query_param_does_not_set_a_cookie(self, client):
        resp = client.get("/?lang=xx")
        assert "Set-Cookie" not in resp.headers

    def test_cookie_is_honored_on_a_later_request_without_query_param(self, client):
        client.set_cookie("lang", "de")
        resp = client.get("/")
        assert b'<html lang="de">' in resp.data

    def test_accept_language_header_is_honored(self, client):
        resp = client.get("/", headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        assert b'<html lang="de">' in resp.data

    def test_query_param_wins_over_accept_language_header(self, client):
        resp = client.get("/?lang=en", headers={"Accept-Language": "de-DE,de;q=0.9"})
        assert b'<html lang="en">' in resp.data

    def test_embedded_i18n_blob_matches_resolved_language(self, client):
        resp = client.get("/?lang=de")
        body = resp.data.decode()
        assert 'const I18N_LANG = "de";' in body
        assert '"bambuddy_url": "Bambuddy-URL"' in body

    def test_unsupported_accept_language_falls_back_to_english(self, client):
        resp = client.get("/", headers={"Accept-Language": "ru-RU,ru;q=0.9"})
        assert b'<html lang="en">' in resp.data

    @pytest.mark.parametrize("lang", i18n.SUPPORTED_LANGS)
    def test_every_supported_language_renders_without_error(self, client, lang):
        resp = client.get(f"/?lang={lang}")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert f'<html lang="{lang}">' in body
        assert "{{" not in body
        assert f'const I18N_LANG = "{lang}";' in body


class TestResolveRequestLocaleHelper:
    def test_matches_i18n_resolve_locale_given_the_same_inputs(self, client):
        with web.app.test_request_context("/?lang=de", headers={"Accept-Language": "en"}):
            resolved = web._resolve_request_locale()
        expected = i18n.resolve_locale("en", query_lang="de", cookie_lang=None)
        assert resolved == expected == "de"


class TestJobControlValidationErrorsAreTranslated:
    """Config-missing / job-state errors are returned as plain JSON `error`
    strings that the frontend toasts verbatim — no config file exists in
    the isolated tmp_path fixture, so every job-start attempt here fails
    validation before ever touching the (mocked-out) sync engine."""

    def test_load_missing_config_error_is_translated(self, client):
        resp = client.post("/api/models/load?lang=de")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"].startswith("Fehlende Konfiguration:")

    def test_sync_missing_config_error_is_translated(self, client):
        resp = client.post("/api/sync/start?lang=de", json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"].startswith("Fehlende Konfiguration:")

    def test_cleanup_missing_config_error_is_translated(self, client):
        resp = client.post("/api/cleanup/start?lang=de", json={})
        assert resp.status_code == 400
        assert resp.get_json()["error"].startswith("Fehlende Konfiguration:")

    def test_cancel_with_no_job_running_error_is_translated(self, client):
        resp = client.post("/api/sync/cancel?lang=de")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "Es läuft kein Job"

    def test_load_missing_config_error_default_language(self, client):
        resp = client.post("/api/models/load")
        assert resp.status_code == 400
        assert resp.get_json()["error"].startswith("Missing config:")


class TestBannerAndSyncJobTranslation:
    """_banner() and SyncJob bind a language once per job (not per request) —
    covers that wiring directly, without needing to mock the whole engine
    and race a background thread."""

    def test_banner_prints_translated_label_and_dry_run_tag(self, capsys):
        job = web.SyncJob("sync", lang="de")
        web._banner("🚀", "log.sync_started_banner", job.t, dry_run=True)
        out = capsys.readouterr().out
        assert "Synchronisierung gestartet" in out
        assert "[TESTLAUF]" in out

    def test_banner_omits_dry_run_tag_when_not_a_dry_run(self, capsys):
        job = web.SyncJob("load", lang="ja")
        web._banner("⟳", "log.loading_models_banner", job.t)
        out = capsys.readouterr().out
        assert "モデルを読み込み中" in out
        assert "ドライラン" not in out  # the dry-run tag text must not appear at all

    def test_sync_job_defaults_to_english(self):
        job = web.SyncJob("load")
        assert job.t("common.title") == "Bambuddy → Manyfold Sync"

    def test_sync_job_binds_requested_language(self):
        job = web.SyncJob("load", lang="fr")
        assert job.t("log.loading_models_banner") == "Chargement des modèles"

    def test_unexpected_error_message_is_translated_via_job(self):
        job = web.SyncJob("sync", lang="es")
        assert job.t("log.unexpected_error", error="boom") == "Error inesperado: boom"
