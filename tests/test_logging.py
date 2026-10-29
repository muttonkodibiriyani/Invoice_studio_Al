import json
import logging

import pytest

from app.logging_setup import JsonFormatter, configure, new_request_id, redact, request_id


@pytest.fixture
def saved_loggers():
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate) for name in names}
    level = logging.getLogger().level
    yield
    for name, (handlers, propagate) in saved.items():
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).propagate = propagate
    logging.getLogger().setLevel(level)


@pytest.mark.parametrize("access_log", [False, True])
def test_json_logs_keep_uvicorn_access_log_setting(access_log, saved_loggers):
    """The image runs uvicorn with --no-access-log; JSON logging must not turn access lines back on."""
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = [logging.StreamHandler()] if access_log else []  # what uvicorn's Config leaves behind
    access.propagate = False
    assert configure(force=True)
    assert access.hasHandlers() is access_log  # uvicorn writes access lines only when this is true
    assert logging.getLogger("uvicorn.error").hasHandlers()
    if access_log:
        assert access.handlers == [] and isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


def test_redact_removes_tokens_codes_and_links():
    text = ("Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
            "visit https://studio.example.test/set-password#oobCode=ABC123 or ?oobCode=XYZ&apiKey=k1 password=hunter2")
    out = redact(text)
    for secret in ("eyJhbGci", "ABC123", "XYZ", "k1", "hunter2", "studio.example.test"):
        assert secret not in out, (secret, out)
    assert "[redacted]" in out


def test_json_formatter_fields_and_redaction():
    formatter = JsonFormatter()
    token = request_id.set("req-1234")
    try:
        record = logging.LogRecord("inv_studio.request", logging.INFO, __file__, 1, "request Bearer abc.def.ghi", None, None)
        record.studio = {"route": "/api/users/{uid}", "status": 200, "user": "u1", "note": "oobCode=SECRET"}
        payload = json.loads(formatter.format(record))
    finally:
        request_id.reset(token)
    assert payload["severity"] == "INFO" and payload["request_id"] == "req-1234"
    assert payload["route"] == "/api/users/{uid}" and payload["status"] == 200 and payload["user"] == "u1"
    assert "SECRET" not in payload["note"] and "abc.def" not in payload["message"]
    assert "time" in payload and "logger" in payload


def test_json_formatter_exception_is_redacted():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom https://example.test/?token=abc")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "startup failed", None, __import__("sys").exc_info())
    payload = json.loads(formatter.format(record))
    assert "ValueError" in payload["exception"] and "token=abc" not in payload["exception"]


@pytest.mark.parametrize("header,expected", [
    (None, None),
    ("105445aa7843bc8bf206b12000100000/1;o=1", "105445aa7843bc8bf206b12000100000"),
    ("short/1", None),
    ("<script>/1", None),
])
def test_request_id_from_trace_header(header, expected):
    rid = new_request_id(header)
    if expected is None:
        assert len(rid) == 32 and rid != header
    else:
        assert rid == expected


def test_request_log_lines_carry_user_route_status_and_latency(client, owner, caplog):
    from tests.conftest import bearer
    with caplog.at_level(logging.INFO, logger="inv_studio.request"):
        client.get("/api/session", headers=bearer(owner))
        client.get("/api/users/nothing-here", headers=bearer(owner))
    lines = [r for r in caplog.records if r.name == "inv_studio.request" and r.getMessage() == "request"]
    assert len(lines) >= 2
    fields = lines[0].studio
    assert fields["route"] == "/api/session" and fields["status"] == 200 and fields["user"] == "owner-uid"
    assert isinstance(fields["latency_ms"], float) and fields["method"] == "GET"
    assert any(r.studio["status"] == 404 for r in lines)
    for record in caplog.records:
        assert "Bearer" not in JsonFormatter().format(record)


def test_startup_failure_is_logged_as_json(tmp_path, monkeypatch, capsys):
    """When create_app raises in cloud mode, the process exits after a structured error line (no traceback-only stderr)."""
    import subprocess, sys, os
    env = {**os.environ, "INV_STUDIO_CLOUD": "1", "INV_STUDIO_JSON_LOGS": "1", "INV_STUDIO_DATA": str(tmp_path / "d")}
    env.pop("GOOGLE_CLOUD_PROJECT", None)
    result = subprocess.run([sys.executable, "-c", "import app.main"], cwd=str(tmp_path.parents[0]) if False else None, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines and lines[-1]["severity"] in ("ERROR", "CRITICAL") and lines[-1]["logger"] == "inv_studio.startup"
    assert "exception" in lines[-1]


def test_only_the_apps_own_value_error_text_reaches_the_caller(tmp_path, caplog):
    import asyncio

    from app.main import create_app
    from app.models import parse_ai_output
    handler = create_app(tmp_path / "data").exception_handlers[ValueError]

    def answer(call):
        try:
            call()
        except ValueError as exc:
            return json.loads(asyncio.run(handler(None, exc)).body)["detail"]

    assert answer(lambda: parse_ai_output("[]")) == "AI output is not an object"
    # The app's compact one-line `if ...:raise ValueError(...)` is still the app's own message.
    from app.excel import rules_workbook
    assert answer(lambda: rules_workbook([], upc="other")) == "UPC mode must be barcode or empty"
    # json's own error names the offending position; a library error like it can name paths or values.
    library = answer(lambda: parse_ai_output("{/srv/secret"))
    assert library.startswith("The request could not be processed (reference ") and "char" not in library
    assert "Expecting property name" in caplog.text
    # A builtin raising from an app line leaves no frame of its own; its text names the value it refused.
    from app.scan_guard import _centre
    builtin = answer(lambda: _centre({"box": ["0", "/srv/secret", "0", "1"]}))
    assert builtin.startswith("The request could not be processed (reference ") and "secret" not in builtin
