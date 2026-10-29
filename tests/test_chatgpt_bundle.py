import json

import pytest

from app.oauth import open_bundle, seal_bundle
from tests.conftest import HEADERS, audit_rows

BUNDLE = {"format": "inv-studio.chatgpt-registration", "credentials": {"refresh_token": "rt-secret-0123456789"}}


def test_a_sealed_registration_opens_only_with_its_passphrase():
    sealed, passphrase = seal_bundle(BUNDLE)
    assert "rt-secret" not in json.dumps(sealed)
    assert len(passphrase.replace("-", "")) * 5 >= 100
    assert open_bundle(sealed, passphrase) == BUNDLE
    assert open_bundle(sealed, f" {passphrase.lower()} ") == BUNDLE
    with pytest.raises(ValueError, match="passphrase is wrong"):
        open_bundle(sealed, "AAAA-" + passphrase[5:])


def test_a_tampered_or_foreign_registration_is_refused_with_one_message():
    sealed, passphrase = seal_bundle(BUNDLE)
    flipped = sealed["ciphertext"][:-6] + ("A" if sealed["ciphertext"][-6] != "A" else "B") + sealed["ciphertext"][-5:]
    for bad in ({**sealed, "ciphertext": flipped}, {**sealed, "version": 2}, BUNDLE, {**sealed, "salt": "*"}):
        with pytest.raises(ValueError, match="^The registration file or its passphrase is wrong"):
            open_bundle(bad, passphrase)


def test_the_export_route_returns_a_sealed_file_and_audits_the_provider_only(tmp_path, monkeypatch):
    monkeypatch.delenv("INV_STUDIO_CLOUD", raising=False)
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.store import Store
    app = create_app(tmp_path / "d", store_type=Store)
    monkeypatch.setattr(app.state.auth, "export_bundle", lambda aid: BUNDLE)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as c:
        response = c.post("/api/chatgpt/export", headers=HEADERS, json={"id": "acct"})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert "rt-secret" not in json.dumps(body["bundle"])
    assert open_bundle(body["bundle"], body["passphrase"]) == BUNDLE
    exported = [payload for event, payload in audit_rows(app) if event == "subscription_exported"]
    assert [json.loads(payload)["provider"] for payload in exported] == ["chatgpt"]
    assert body["passphrase"] not in exported[0] and "rt-secret" not in exported[0]
