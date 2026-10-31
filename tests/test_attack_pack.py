"""The aggressive test pack (scripts/attack_pack.py) against the real app in cloud mode with the stand-in tenant.

A pack that passes a broken app is worse than no pack, so besides "the current app passes", each check family is
run against an app broken on purpose and must FAIL there. The slow groups (hostile uploads up to the 240 s reader
limit, the engine reads) run against a live server by hand: docs/ATTACK_PACK.md.
"""
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as studio
from app.authorization import ATTRIBUTE, PUBLIC
from app.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import attack_pack  # noqa: E402

FAST = {"edge", "tokens", "csrf", "authz", "plan", "load"}


class BlobStore(Store):
    """The cloud upload path writes each upload to blob storage; here the local file is the blob."""
    def persist_blob(self, path): pass
    def ensure_blob(self, path): pass
    def remove_blob(self, path): pass


@pytest.fixture
def cloud_app(tmp_path, identity):
    return studio.create_app(tmp_path / "data", identity=identity, store_type=BlobStore)


@pytest.fixture
def pack(cloud_app, tenant, monkeypatch):
    """The pack, with its HTTP client pointed at the app in process; the tenant's real kid, and no `az`."""
    monkeypatch.setattr(attack_pack, "_tenant_kids", lambda tenant_id: [tenant.kid])
    monkeypatch.setattr(attack_pack, "_az_token", lambda resource: None)
    lines = []
    p = attack_pack.Pack("http://127.0.0.1", tenant.token("admin-oid", "a@example.test", roles=("admin",)),
                         tenant.token("member-oid", "m@example.test"), wait=60, out=lines.append)
    p.http = TestClient(cloud_app, base_url="http://127.0.0.1", follow_redirects=False)
    p.load = lambda: attack_pack.Pack.load(p, workers=4, requests=20)
    return p


def failed(results, group=None):
    return [f"{r['group']}: {r['case']} — {r['detail']}" for r in results
            if r["status"] == "FAIL" and (group is None or r["group"] == group)]


def test_the_route_list_is_every_signed_in_api_route_at_its_level(cloud_app):
    actual = set()
    for route in cloud_app.router.routes:
        level = getattr(getattr(route, "endpoint", None), ATTRIBUTE, None)
        if getattr(route, "path", "").startswith("/api/") and level != PUBLIC:
            for method in route.methods - {"HEAD"}:
                actual.add((method, re.sub(r"\{[^}]+\}", "x", route.path), level))
    listed = {(m, re.sub(r"/(openai)(?=/|$)", "/x", p), level) for m, p, level in attack_pack.ROUTES}
    assert listed == actual, {"missing": sorted(actual - listed), "not in the app": sorted(listed - actual)}


def test_the_current_app_passes_every_fast_group_and_nothing_is_left_behind(pack, cloud_app):
    results = pack.run(FAST)
    assert not failed(results)
    assert {r["group"] for r in results if r["status"] == "PASS"} >= FAST
    with cloud_app.state.store.connection() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0, "the cleanup left invoices"


def test_a_missing_security_header_fails(pack, cloud_app):
    @cloud_app.middleware("http")
    async def strip(request, call_next):
        response = await call_next(request)
        del response.headers["content-security-policy"]
        return response
    assert any("security headers" in f for f in failed(pack.run({"edge"}), "edge"))


def test_an_app_that_accepts_any_token_fails_the_token_and_route_checks(pack, cloud_app, monkeypatch):
    monkeypatch.setattr(cloud_app.state.identity_provider, "verify",
                        lambda authorization: {"uid": "anyone", "email": "x@example.test", "name": "x", "role": "admin"})
    bad = failed(pack.run({"tokens"}), "tokens")
    assert any("answer 401 with no token" in f for f in bad)
    assert any("own RSA key, real tenant kid" in f for f in bad)
    assert any("alg none" in f for f in bad)


def test_an_app_that_lets_a_member_use_admin_routes_fails(pack, monkeypatch):
    monkeypatch.setattr(studio, "allows", lambda level, role: True)
    assert any("admin routes refuse a member" in f for f in failed(pack.run({"authz"}), "authz"))


def test_an_app_without_the_request_protection_header_check_fails(pack, cloud_app):
    @cloud_app.middleware("http")
    async def add_header(request, call_next):
        # Every request now carries the header, as if the app no longer required it.
        request.scope["headers"] = [*request.scope["headers"], (b"x-studio-request", b"1")]
        return await call_next(request)
    assert any("without X-Studio-Request" in f for f in failed(pack.run({"csrf"}), "csrf"))


def test_the_field_comparison_is_the_golden_samples_one():
    expected = {"number": "A-1", "net": "800", "lines": [{"qty": "10", "description": "Gel pen 0.5 mm"}]}
    assert attack_pack._compare({"number": "A-1", "net": "800.00", "lines": [{"qty": "10.0", "description": "Gel pen 0.5 mm"}]}, expected) == []
    assert attack_pack._compare({"number": "A-l", "net": "800", "lines": [{"qty": "10", "description": "Gel pen 0.5 mm"}]}, expected)
    assert attack_pack._compare({"number": "A-1", "net": "800", "lines": [{"qty": "10", "description": "Gel pen 0.5mm"}]}, expected)
    assert attack_pack._compare({"number": "A-1", "net": "800", "lines": []}, expected) == ["0 lines, want 1"]
    assert attack_pack._compare({"number": "A-1", "net": None, "lines": [{"qty": "10", "description": "Gel pen 0.5 mm"}]}, expected)


@pytest.mark.parametrize("job,want", [
    ({"status": "error", "error": "Processing failed", "trace": []}, "error: Processing failed"),
    ({"status": "processing", "trace": []}, "still processing"),
    ({"status": "review", "trace": [], "readers": {"ai": {"calls": 1}}, "invoice": {}}, "AI was not called"),
])
def test_an_engine_job_that_errored_stalled_or_asked_the_ai_fails(pack, monkeypatch, job, want):
    monkeypatch.setattr(pack, "wait_job", lambda jid: job)
    pack.judge("invoice.pdf by auto", "invoice.pdf", attack_pack.LOCAL_ONLY, True, "jid", time.monotonic())
    assert any(want in f"{r['case']} {r['detail']}" for r in pack.results if r["status"] == "FAIL"), pack.results


def test_a_forced_engine_that_did_not_read_fails(pack, monkeypatch):
    monkeypatch.setattr(pack, "wait_job", lambda jid: {"status": "review", "readers": {"ai": {"calls": 0}},
                                                       "trace": [{"engine": "docling", "status": "models_unavailable"}]})
    pack.judge("scan by docling", "invoice-scan.png", {**attack_pack.LOCAL_ONLY, "engine": "docling"}, False, "jid", time.monotonic())
    assert failed(pack.results) == ["engines: scan by docling: the chosen engine read the file — 0s after upload, trace [('docling', 'models_unavailable')]"]


def test_the_hostile_files_are_what_they_claim():
    from PIL import Image
    import io
    import pypdfium2 as pdfium
    assert len(pdfium.PdfDocument(attack_pack._pdf_pages(21))) == 21
    assert len(pdfium.PdfDocument(attack_pack._pdf_xobject_bomb(40))) == 1
    with Image.open(io.BytesIO(attack_pack._png(6000, 6000))) as image:
        assert image.size == (6000, 6000)
    with Image.open(io.BytesIO(attack_pack._tiff_frames(25))) as image:
        assert image.n_frames == 25
    import zipfile
    with zipfile.ZipFile(io.BytesIO(attack_pack._zip_bomb("word/document.xml", 31_000_000))) as archive:
        assert archive.getinfo("word/document.xml").file_size == 31_000_000
