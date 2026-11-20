"""Tests for the POST /api/* request guard in bambuddy_manyfold_web.py."""

import pytest

import bambuddy_manyfold_web as web

URL = "/api/sync/cancel"  # cheap, side-effect free: 409 when no job runs
HOST = "localhost"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "WEB_CONFIG_FILE", tmp_path / "web_config.json")
    monkeypatch.setattr(web, "_current_job", None)
    monkeypatch.delenv("B2M_API_TOKEN", raising=False)
    web.app.testing = True
    return web.app.test_client()


def _post(client, **headers):
    return client.post(URL, headers=headers, base_url=f"http://{HOST}")


def _allowed(resp):
    return resp.status_code not in (401, 403)


class TestTokenUnset:
    def test_no_origin_allowed(self, client):
        assert _allowed(_post(client))

    def test_same_origin_allowed(self, client):
        assert _allowed(_post(client, Origin=f"https://{HOST}"))

    @pytest.mark.parametrize("scheme", ["safari-web-extension", "chrome-extension", "moz-extension"])
    def test_extension_origin_allowed(self, client, scheme):
        assert _allowed(_post(client, Origin=f"{scheme}://abc-123"))

    def test_foreign_origin_forbidden(self, client):
        resp = _post(client, Origin="https://evil.example")
        assert resp.status_code == 403
        assert resp.get_json() == {"ok": False, "error": "forbidden origin"}

    def test_null_origin_forbidden(self, client):
        assert _post(client, Origin="null").status_code == 403


class TestTokenSet:
    @pytest.fixture(autouse=True)
    def _token(self, client, monkeypatch):
        monkeypatch.setenv("B2M_API_TOKEN", "s3cret")

    def test_bearer_ok(self, client):
        assert _allowed(_post(client, Authorization="Bearer s3cret", Origin="https://evil.example"))

    def test_x_api_key_ok(self, client):
        assert _allowed(_post(client, **{"X-API-Key": "s3cret"}))

    def test_wrong_token_foreign_origin_401(self, client):
        resp = _post(client, Authorization="Bearer nope", Origin="https://evil.example")
        assert resp.status_code == 401
        assert resp.get_json() == {"ok": False, "error": "unauthorized"}

    def test_no_token_no_origin_401(self, client):
        assert _post(client).status_code == 401

    def test_extension_origin_without_token_401(self, client):
        assert _post(client, Origin="safari-web-extension://abc").status_code == 401

    def test_same_origin_without_token_allowed(self, client):
        assert _allowed(_post(client, Origin=f"http://{HOST}"))

    def test_wrong_token_same_origin_allowed(self, client):
        assert _allowed(_post(client, Authorization="Bearer nope", Origin=f"http://{HOST}"))


def test_get_unaffected(client, monkeypatch):
    monkeypatch.setenv("B2M_API_TOKEN", "s3cret")
    resp = client.get("/api/sync/status", headers={"Origin": "https://evil.example"})
    assert _allowed(resp)
