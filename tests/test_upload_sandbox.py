import io
import json
import os
import signal
import subprocess
import sys

import pytest

from app import upload_check

POSIX = pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX only")
LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and prctl are Linux only")


def child(code, *args):
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, timeout=60)


@POSIX
def test_a_limited_child_is_stopped_at_its_cpu_cap():
    proc = child("from app.sandbox import limit\nlimit(1)\nwhile True: pass")
    assert proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL)


@POSIX
def test_a_limited_child_cannot_grow_past_its_address_space_cap():
    proc = child("from app.sandbox import limit\nlimit(10, 256)\nbytearray(512 * 1024 * 1024)")
    assert proc.returncode == 1 and "MemoryError" in proc.stderr


@LINUX
def test_a_reader_cannot_read_the_api_process_environment(tmp_path):
    """In the cloud the API process holds the database URL and the vault key in its environment."""
    script = """
import os, subprocess, sys
from app.main import create_app
from app.store import Store
create_app(sys.argv[1], store_type=Store)
read = "import os; open(f'/proc/{os.getppid()}/environ', 'rb').read()"
proc = subprocess.run([sys.executable, "-c", read], capture_output=True, text=True)
print("PermissionError" in proc.stderr, proc.returncode, flush=True)
os._exit(0)
"""
    env = {k: v for k, v in os.environ.items() if k != "INV_STUDIO_CLOUD"}
    proc = subprocess.run([sys.executable, "-c", script, str(tmp_path / "d")], capture_output=True, text=True,
                          timeout=120, env=env)
    assert proc.stdout.split() == ["True", "1"], proc.stderr[-2000:]


def png(size=(4, 4)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


def test_uploads_are_opened_in_the_child_not_in_the_api_process(tmp_path, monkeypatch):
    from tests.conftest import bearer
    from tests.test_batch import OPTIONS, FakeReader, studio

    import pypdfium2
    from PIL import Image
    from app import multi_invoice
    in_process = []
    for owner, name in ((upload_check, "inspect"), (Image, "open"), (pypdfium2, "PdfDocument"),
                        (multi_invoice, "analyse_pdf")):
        def record(*args, _name=name, **kwargs):
            in_process.append(_name)
            raise RuntimeError(f"{_name} ran inside the API process")
        monkeypatch.setattr(owner, name, record)
    app, client, token = studio(tmp_path, monkeypatch, FakeReader())
    files = {"a.png": png(), "b.png": b"\x89PNG\r\n\x1a\nnot an image", "c.pdf": b"%PDF-1.4 broken"}
    plan = client.post("/api/preflight", headers=bearer(token),
                       json={"options": OPTIONS, "files": [{"name": n, "size": len(c)} for n, c in files.items()]})
    plan_token = plan.json()["token"]
    assert client.post("/api/preflight/confirm", json={"token": plan_token}, headers=bearer(token)).status_code == 200
    details = {}
    for name, content in files.items():
        response = client.post("/api/invoices", files={"file": (name, content, "application/octet-stream")},
                               data={"options": json.dumps(OPTIONS), "preflight_token": plan_token}, headers=bearer(token))
        details[name] = (response.status_code, response.json().get("detail"))
    assert details["a.png"][0] == 200, details
    assert details["b.png"] == (400, upload_check.MESSAGES["image"]), details
    assert details["c.pdf"] == (400, upload_check.MESSAGES["pdf"]), details
    assert in_process == []


def test_the_child_answers_each_kind_of_bad_file_with_its_own_message():
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    upload_check.check(png(), ".png", env)
    with pytest.raises(ValueError, match="^Image is invalid"):
        upload_check.check(png((5001, 5001)), ".png", env)
    with pytest.raises(ValueError, match="^PDF is damaged"):
        upload_check.check(b"%PDF-1.4 broken", ".pdf", env)


def test_readers_get_a_cpu_cap_and_an_address_space_cap_only_when_configured(tmp_path, monkeypatch):
    from app import engines
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(engines.subprocess, "run", run)
    for value in (None, "6144"):
        if value:
            monkeypatch.setenv("INV_STUDIO_READER_MAX_AS_MB", value)
        with pytest.raises(ValueError, match="timed out"):
            engines.local_read("docling", tmp_path / "a.pdf", tmp_path)
    flags = [dict(zip(cmd[1::], cmd[2::])) for cmd in seen]
    assert all(int(f["--cpu-seconds"]) >= 300 for f in flags)
    assert [f["--max-as-mb"] for f in flags] == ["0", "6144"]
