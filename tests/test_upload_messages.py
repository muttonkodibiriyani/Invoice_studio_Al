"""A clerk reads what is wrong with a file: too large, empty, password-protected and damaged all get their own words."""

import io
import json

from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from app.main import MAX_UPLOAD
from tests.conftest import bearer
from tests.test_batch import studio

OPTIONS = {"engine": "auto", "ai_fallback": False, "provider": "openai", "model": "", "language": "en"}


def _pdf(**kwargs):
    out = io.BytesIO()
    page = canvas.Canvas(out, **kwargs)
    page.drawString(40, 800, "Made-up page")
    page.showPage()
    page.save()
    return out.getvalue()


def _upload(client, name, content):
    plan = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": name, "size": len(content)}]}).json()
    client.post("/api/preflight/confirm", json={"token": plan["token"]}).raise_for_status()
    return client.post("/api/invoices", files={"file": (name, content, "application/pdf")},
                       data={"options": json.dumps(OPTIONS), "preflight_token": plan["token"]})


def _client(tmp_path, monkeypatch):
    _, client, token = studio(tmp_path, monkeypatch, lambda *a, **k: {})
    client.headers.update(bearer(token))
    return client


def test_plan_refuses_a_large_or_empty_file_in_plain_words(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        large = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": "big.pdf", "size": MAX_UPLOAD + 1}]})
        assert large.status_code == 422 and large.json()["detail"].startswith("This file is over the 12 MB limit.")
        empty = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": "none.pdf", "size": 0}]})
        assert empty.status_code == 422 and empty.json()["detail"].startswith("This file is empty.")
        # Any other invalid request keeps its exact message.
        other = client.post("/api/preflight", json={"options": OPTIONS, "files": [{"name": "x.pdf", "size": "many"}]})
        assert other.status_code == 422 and other.json()["detail"].startswith("Invalid input: body.files.0.size")


def test_password_and_damage_are_told_apart(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        locked = _upload(client, "locked.pdf", _pdf(encrypt=StandardEncryption("made-up-secret", canPrint=0)))
        assert locked.status_code == 400 and "protected by a password" in locked.json()["detail"], locked.text
        for damaged in (b"%PDF-1.7\n" + b"\x13\x37" * 2000, _pdf()[:200]):
            response = _upload(client, "damaged.pdf", damaged)
            assert response.status_code == 400 and "damaged" in response.json()["detail"] and "password" not in response.json()["detail"], response.text
