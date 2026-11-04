"""docs/API.md, the Postman collection and the PDF describe exactly the routes the app serves, at the levels the app
enforces, and are regenerated whenever that changes (scripts/build_api_docs.py)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_api_docs  # noqa: E402


@pytest.fixture(scope="module")
def entries():
    return build_api_docs.documented()


def test_every_served_route_is_documented_and_nothing_else(entries):
    served = {(m, p) for m, p, _ in build_api_docs.app_routes()} - build_api_docs.NOT_API
    assert {(e["method"], e["path"]) for e in entries} == served


def test_a_route_missing_from_the_catalogue_stops_the_build():
    routes = build_api_docs.app_routes() + [("GET", "/api/new-thing", "member")]
    with pytest.raises(SystemExit, match="Not documented: \\[\\('GET', '/api/new-thing'\\)\\]"):
        build_api_docs.documented(routes)


def test_the_levels_are_the_apps(entries):
    levels = {(e["method"], e["path"]): e["level"] for e in entries}
    assert levels[("GET", "/api/public-config")] == "public"
    assert levels[("GET", "/health")] == "none"
    assert levels[("GET", "/api/users")] == "admin" and levels[("POST", "/api/jobs/delete")] == "admin"
    assert levels[("GET", "/api/state")] == "member"
    assert set(levels.values()) <= {"public", "member", "admin", "none"}


def test_the_written_files_are_up_to_date(entries):
    for path, data in build_api_docs.outputs(entries).items():
        assert path.read_bytes() == data, f"{path.name} is stale: run scripts/build_api_docs.py"
    assert build_api_docs.PDF.read_bytes()[:5] == b"%PDF-"


def test_the_collection_covers_every_route_and_sends_the_write_header(entries):
    collection = json.loads(build_api_docs.COLLECTION.read_text())
    folders = {f["name"]: f for f in collection["item"]}
    assert list(folders) == ["1. Smoke test", "2. Security checks", "3. Invoice flow", "4. API reference"]
    reference = [item for area in folders["4. API reference"]["item"] for item in area["item"]]
    assert len(reference) == len(entries)
    for item in reference:
        request = item["request"]
        if request["method"] != "GET":
            assert {"key": "X-Studio-Request", "value": "1"} in request["header"], item["name"]
    assert collection["auth"]["bearer"][0]["value"] == "{{token}}"
    environment = json.loads(build_api_docs.ENVIRONMENT.read_text())
    secret = {v["key"] for v in environment["values"] if v["type"] == "secret"}
    assert secret == {"token", "memberToken"} and all(not v["value"] for v in environment["values"] if v["key"] in secret)


def test_the_sample_upload_matches_the_plan():
    collection = json.loads(build_api_docs.COLLECTION.read_text())
    flow = next(f for f in collection["item"] if f["name"] == "3. Invoice flow")["item"]
    plan = json.loads(flow[0]["request"]["body"]["raw"])
    assert plan["files"] == [{"name": "invoice.pdf", "size": (ROOT / "samples" / "invoice.pdf").stat().st_size}]
    upload = {f["key"]: f for f in flow[2]["request"]["body"]["formdata"]}
    assert upload["file"]["src"] == "samples/invoice.pdf"
    assert json.loads(upload["options"]["value"]) == plan["options"]
    assert plan["options"]["ai_fallback"] is False and plan["options"]["ai_cross_check"] is False
