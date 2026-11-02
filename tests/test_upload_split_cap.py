"""Attack pack F1: the upload's multi-invoice check has its own short time limit and a cap on how many run at once,
so PDFs whose page draws itself 2^40 times cannot hold every request thread. A hostile upload past the cap is
answered 429 at once, and the app still answers /api/state and another account's normal upload meanwhile."""
import json
import sys
import threading
import time
from pathlib import Path

from app import engines
from tests.conftest import Tenant, bearer
from tests.test_batch import OPTIONS, FakeReader, studio
from tests.test_upload_split_limit import BOUND, upload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from attack_pack import _pdf_xobject_bomb  # noqa: E402

BOMB = _pdf_xobject_bomb(40)


def hostile(client, token):
    """The bomb, uploaded as the attack pack does: no plan, since the check runs before the plan is looked at."""
    started = time.monotonic()
    response = client.post("/api/invoices", files={"file": ("invoice.pdf", BOMB, "application/pdf")},
                           data={"options": json.dumps(OPTIONS)}, headers=bearer(token))
    return response, time.monotonic() - started


def test_hostile_pdfs_are_refused_within_the_limit_and_cannot_hold_every_slot(tmp_path, monkeypatch):
    monkeypatch.delenv("INV_SPLIT_CHECK_TIMEOUT", raising=False)
    monkeypatch.setattr(engines, "SPLIT_CHECK_CONCURRENCY", 2)  # so one account may hold one check at a time
    app, client, owner = studio(tmp_path, monkeypatch, FakeReader())
    member = Tenant().token("member-uid", "member@example.test", roles=("member",))
    assert client.get("/api/session", headers=bearer(member)).status_code == 200

    held = []
    first = threading.Thread(target=lambda: held.append(hostile(client, member)))
    first.start()
    deadline = time.monotonic() + 15
    while engines._split_checks.get("member-uid") != 1:
        assert time.monotonic() < deadline, "the first hostile upload never reached the split check"
        time.sleep(0.05)

    # More hostile uploads from the same account get no slot: answered 429 at once, holding no thread.
    flood = [None] * 5
    threads = [threading.Thread(target=lambda n=n: flood.__setitem__(n, hostile(client, member))) for n in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert [(r.status_code, r.headers.get("retry-after")) for r, _ in flood] == [(429, "5")] * 5
    assert max(seconds for _, seconds in flood) < 5

    # While the first check is still running, the app answers everyone else.
    started = time.monotonic()
    assert client.get("/api/state", headers=bearer(owner)).status_code == 200
    assert time.monotonic() - started < 5
    started = time.monotonic()
    response = upload(client, owner, "bound.pdf", BOUND)
    assert response.status_code == 200, response.text
    assert time.monotonic() - started < 15
    assert first.is_alive(), "the hostile check was still running while the others were answered"

    first.join(40)
    (response, seconds), = held
    assert response.status_code == 400 and seconds < 30, (response.status_code, seconds)
    assert response.json()["detail"].startswith("This PDF could not be checked for several invoices")
    assert engines._split_checks == {}, "every slot is given back"


def test_the_limit_and_the_cap_come_from_the_environment(monkeypatch):
    for value, want in (("", 20), ("x", 20), ("0", 1), ("45", 45), ("9999", 240)):
        monkeypatch.setenv("INV_SPLIT_CHECK_TIMEOUT", value)
        assert engines.split_check_seconds() == want
    assert engines.SPLIT_CHECK_CONCURRENCY == engines.concurrency_from_env("INV_SPLIT_CHECK_CONCURRENCY", 4, 16)
