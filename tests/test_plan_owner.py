"""Attack pack F2: a confirmed processing plan serves only the account that made it."""
from tests.conftest import Tenant, bearer
from tests.test_batch import OPTIONS, FakeReader, content, plan, studio, upload


def member_token():
    return Tenant().token("member-uid", "member@example.test", roles=("member",))


def test_another_account_cannot_upload_under_a_confirmed_plan(tmp_path, monkeypatch):
    reader = FakeReader()
    app, client, admin = studio(tmp_path, monkeypatch, reader)
    plan_token = plan(client, admin, ["a.txt"])
    other = member_token()
    response = upload(client, other, plan_token, "a.txt")
    assert response.status_code == 409, response.text
    assert app.state.store.jobs() == [] and reader.calls == []
    # The maker still can.
    assert upload(client, admin, plan_token, "a.txt").status_code == 200


def test_another_account_cannot_confirm_or_renew_a_plan(tmp_path, monkeypatch):
    app, client, admin = studio(tmp_path, monkeypatch, FakeReader())
    files = [{"name": "a.txt", "size": len(content("a.txt"))}]
    token = client.post("/api/preflight", json={"options": OPTIONS, "files": files}, headers=bearer(admin)).json()["token"]
    other = member_token()
    assert client.post("/api/preflight/confirm", json={"token": token}, headers=bearer(other)).status_code == 409
    assert client.post("/api/preflight/confirm", json={"token": token}, headers=bearer(admin)).status_code == 200
    assert client.post("/api/preflight/renew", json={"token": token}, headers=bearer(other)).status_code == 409
    assert client.post("/api/preflight/renew", json={"token": token}, headers=bearer(admin)).status_code == 200
