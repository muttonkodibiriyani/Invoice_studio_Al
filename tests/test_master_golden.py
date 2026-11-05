"""Golden final rows for the 14 real customer invoices against the real item master (rules page v0.3).

The invoices and the master are private and never in git. Point INV_MASTER_GOLDEN_DIR at a directory with
``master.xlsx``, ``golden.key``, ``invoices/<stem>.json`` (truth reads in the samples shape) and
``confirmations.json`` ({stem: {"supplier_name": ...}}: the SUPPLIER SITE NAME a reviewer confirms, v0.3 step 1);
without it every case skips. tests/fixtures/master_golden/expected.json holds the expected cells; private values (supplier site,
item, order, location, invoice) are HMAC-SHA256 under golden.key, so the fixture pins them without showing them.

Each invoice runs from its truth read, not OCR, so only the item master rules are under test, with the reviewer's
supplier confirmation applied (v0.3 searches items only inside the confirmed site). A cell with
``confirm: false`` must equal the golden value. A cell with ``confirm: true`` waits for a reviewer: it may be
blank or already hold the golden value, never another value, and the invoice may not be Approved.

Every case passes since the rule fixes in PR #29. The expected values still wait for the business owner's sign-off
(task "Confirm the expected outputs are right"); the assumed ones are listed in doc 01a1a88a-0301.
"""
import hashlib
import hmac
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.fine_rules import RulesConfig, run_invoice
from app.fine_rules_source import LookupRulesSource
from app.main import create_app
from app.models import Invoice
from app.store import Store
from tests.conftest import OWNER, Tenant, bearer, entra_env

FIXTURE = Path(__file__).parent / "fixtures" / "master_golden" / "expected.json"
EXPECTED = json.loads(FIXTURE.read_text())
PRIVATE = os.environ.get("INV_MASTER_GOLDEN_DIR")
# Location Type keeps the target-template text (coordinator decision, 4 Nov); v0.3 writes Store(S) / Warehouse(W).
PUBLIC_VALUES = {"", "Store (S)", "Warehouse (W)", "UAEV5", "KSAV15"}
# Codes from the rules page v0.3, still assumed until Tarkesh sends the final ones.
CONFIG = {"version": "golden-v0.3", "vat_codes": [
    {"region": "ARE", "code": "UAEV5", "rate": "5", "active_from": "2020-01-01"},
    {"region": "SAU", "code": "KSAV15", "rate": "15", "active_from": "2020-01-01"}]}


@pytest.fixture(scope="module")
def private_dir():
    if not PRIVATE or not (Path(PRIVATE) / "master.xlsx").is_file():
        pytest.skip("INV_MASTER_GOLDEN_DIR with the private master and invoices is not set")
    return Path(PRIVATE)


@pytest.fixture(scope="module")
def key(private_dir):
    return (private_dir / "golden.key").read_text().strip().encode()


@pytest.fixture(scope="module")
def invoices(private_dir, key):
    """Golden invoice digest -> truth file."""
    return {digest(key, p.stem): p for p in (private_dir / "invoices").glob("*.json")}


@pytest.fixture(scope="module")
def confirmations(private_dir):
    path = private_dir / "confirmations.json"
    return {stem: {"supplier_name": c["supplier_name"]} for stem, c in json.loads(path.read_text()).items()} \
        if path.is_file() else {}


@pytest.fixture(scope="module")
def source(private_dir, tmp_path_factory):
    """One app with the real master activated through the API, shared by all 14 cases (activation is slow)."""
    tenant = Tenant()
    with pytest.MonkeyPatch.context() as monkeypatch:
        entra_env(monkeypatch)
        from app.authentication import EntraIdentity
        app = create_app(tmp_path_factory.mktemp("golden") / "data", identity=EntraIdentity(fetch=tenant.jwks),
                         store_type=Store)
        with TestClient(app) as client:
            token = tenant.token("owner-uid", OWNER, roles=("admin",))
            assert client.get("/api/session", headers=bearer(token)).status_code == 200
            response = client.post("/api/item-master/validate", headers=bearer(token),
                                   files={"file": ("master.xlsx", (private_dir / "master.xlsx").read_bytes())})
            assert response.status_code == 200, response.text[:500]
            upload = response.json()["upload_id"]
            response = client.post(f"/api/item-master/uploads/{upload}/activate", headers=bearer(token))
            assert response.status_code == 200, response.text[:500]
        yield LookupRulesSource(app.state.store)


def digest(key, value):
    return "hmac:" + hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def matches(key, actual, expected):
    actual = "" if actual is None else str(actual)
    return actual == expected if expected in PUBLIC_VALUES else digest(key, actual) == expected


def shown(key, value, expected):
    """Mismatch text that never prints a private value."""
    value = "" if value is None else str(value)
    return repr(value) if value in PUBLIC_VALUES else "<private>"


def check(key, where, cell, actual):
    expected = cell["value"]
    if matches(key, actual, expected) or (cell["confirm"] and str(actual or "") == ""):
        return []
    want = "blank or the golden value" if cell["confirm"] else repr(expected) if expected in PUBLIC_VALUES else "<private>"
    return [f"{where} [{cell['rule']}]: got {shown(key, actual, expected)}, want {want}"]


def run(path, source, confirmations):
    data = {**json.loads(path.read_text()), **confirmations.get(path.stem, {})}
    invoice = Invoice(**data)
    # The issuer heads the page, as on the printed invoice; the rest is the truth read itself.
    text_value = "\n".join([data.get("supplier_name") or "", f"Invoice {invoice.number}"] +
                           [line.description or "" for line in invoice.lines])
    return run_invoice(invoice, source, RulesConfig.from_dict(CONFIG), filename=path.name,
                       text_value=text_value)



@pytest.mark.parametrize("case", EXPECTED["cases"], ids=lambda c: c["case"])
def test_master_golden(case, source, invoices, key, confirmations):
    path = invoices.get(case["invoice"])
    assert path, f"{case['case']}: invoice not found in INV_MASTER_GOLDEN_DIR/invoices"
    result = run(path, source, confirmations)
    problems = []
    for name, cell in case["header"].items():
        problems += check(key, name, cell, result["header"].get(name))
    assert len(result["lines"]) == len(case["lines"]), f"{case['case']}: line count"
    for row, golden in zip(result["lines"], case["lines"]):
        for name in ("Item", "Unit Tax Code"):
            problems += check(key, f"line {golden['n']} {name}", golden[name], row.get(name))
    found = sum(1 for row, golden in zip(result["lines"], case["lines"])
                if not golden["Item"]["confirm"] and matches(key, row.get("Item"), golden["Item"]["value"]))
    coverage = case["coverage"]
    percent = float((Decimal(100) * found / len(case["lines"])).quantize(Decimal("0.01")))
    if percent != coverage["percent"]:
        problems.append(f"coverage: got {found}/{len(case['lines'])} ({percent}%), want {coverage['found']}/"
                        f"{coverage['lines']} ({coverage['percent']}%, tier {coverage['tier']})")
    if result["status"] == "Approved" and any(c["confirm"] for c in case["header"].values()):
        problems.append("status: Approved although cells still need a reviewer's confirmation")
    assert not problems, f"{case['case']} differs from the golden output:\n  " + "\n  ".join(problems)


def test_fixture_is_redacted_and_complete():
    """Runs without the private data: the fixture names no private value and covers every invoice once."""
    cases = EXPECTED["cases"]
    assert len(cases) == 14 and len({c["invoice"] for c in cases}) == 14
    for case in cases:
        cells = list(case["header"].values()) + [c for line in case["lines"] for c in (line["Item"], line["Unit Tax Code"])]
        for cell in cells:
            assert cell["value"] in PUBLIC_VALUES or cell["value"].startswith("hmac:"), case["case"]
        assert case["coverage"]["lines"] == len(case["lines"])
        assert case["coverage"]["found"] == sum(not line["Item"]["confirm"] for line in case["lines"])
