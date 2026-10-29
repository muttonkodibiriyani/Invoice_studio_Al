"""A scan reader downloads its models the first time it runs. When a network refuses that download,
the reader can read nothing until the network is fixed, so it must be skipped with that reason —
not waited for on every invoice and not reported as a document that could not be read."""
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import engines  # noqa: E402
from app.models import ProcessingOptions  # noqa: E402

REFUSED = engines.DOWNLOAD_REFUSED


@pytest.fixture(autouse=True)
def forget():
    engines.forget_models_blocked()
    yield
    engines.forget_models_blocked()


@pytest.fixture
def reading(monkeypatch, tmp_path):
    """Run the local reader chain over one scan, with every reader installed and none of them real."""
    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": engine, "installed": True, "models_blocked": False, "version": "1", "note": ""}
        for engine in ("invoice2data", "paddleocr", "docling")])
    (tmp_path / "work").mkdir()
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"\x89PNG\r\n\x1a\n")
    calls = []

    def run(fails_with=REFUSED):
        def local_read(engine, path, root, language="en", extra=()):
            calls.append(engine)
            if engine == "docling":
                raise engines.ReaderFailed("This picture could not be read.", fails_with)
            return {"text": "", "invoice": None, "boxes": [], "tables": []}

        monkeypatch.setattr(engines, "local_read", local_read)
        calls.clear()
        result = engines.process(scan, ProcessingOptions(engine="auto"),
                                 types.SimpleNamespace(root=tmp_path), None)
        entry = next(t for t in result["trace"] if t["engine"] == "docling")
        return calls, entry

    return run


def test_a_refused_model_download_is_not_waited_for_again_on_the_next_invoice(reading):
    first, entry = reading()
    assert first == ["invoice2data", "paddleocr", "docling"]
    assert entry["status"] == "models_unavailable" and entry["ocr_status"] == "models_unavailable"
    assert "could not download its models" in entry["reason"]
    assert "Nothing is wrong with this document" in entry["reason"], "not a bad read"

    second, entry = reading()
    assert "docling" not in second, "the same refused download would cost every invoice minutes"
    assert entry["status"] == "models_unavailable" and "Skipped" in entry["reason"]


def test_the_reader_is_tried_again_once_the_window_passes_so_a_fixed_network_needs_no_restart(
        reading, monkeypatch):
    reading()
    assert engines.models_blocked("docling") is not None
    monkeypatch.setattr(engines, "MODELS_BLOCKED_SECONDS", 0)
    assert engines.models_blocked("docling") is None
    tried, _ = reading()
    assert "docling" in tried


def test_a_reader_that_failed_on_the_document_itself_is_still_tried_on_the_next_invoice(reading):
    reading(fails_with="ValueError")
    assert engines.models_blocked("docling") is None
    tried, entry = reading(fails_with="ValueError")
    assert "docling" in tried
    assert entry["status"] == "failed" and entry["ocr_status"] == "failed"


def test_a_timeout_while_reading_does_not_block_the_reader(reading):
    """Only an error from the model-fetching code is a refused download, whatever its class."""
    reading(fails_with="TimeoutError")
    assert engines.models_blocked("docling") is None
    tried, entry = reading(fails_with="TimeoutError")
    assert "docling" in tried
    assert entry["status"] == "failed" and "could not download" not in entry["reason"]


def raised_in(filename, error):
    try:
        exec(compile(f"raise {error}", filename, "exec"))
    except Exception as raised:
        return raised


@pytest.mark.parametrize("filename, error", [
    ("/v/site-packages/huggingface_hub/file_download.py", "Exception('LocalEntryNotFoundError')"),
    ("/v/site-packages/huggingface_hub/utils/_http.py", "TimeoutError()"),
    ("C:/v/Lib/site-packages/paddlex/inference/utils/official_models.py",
     "Exception('No model source is available!')"),
])
def test_an_error_raised_while_fetching_models_is_a_refused_download(filename, error):
    from app import ocr_worker

    assert ocr_worker._model_download_refused(raised_in(filename, error))


@pytest.mark.parametrize("filename, error", [
    ("/v/site-packages/paddlex/inference/models/base/predictor.py", "TimeoutError()"),
    ("/v/site-packages/docling/pipeline/standard_pdf_pipeline.py", "ConnectionResetError()"),
    ("/v/site-packages/paddlex/inference/utils/io.py", "Exception('No model source is available!')"),
])
def test_the_same_error_raised_anywhere_else_is_not(filename, error):
    from app import ocr_worker

    assert not ocr_worker._model_download_refused(raised_in(filename, error))


def test_the_engine_list_the_browser_reads_says_a_reader_is_installed_but_has_no_models(monkeypatch):
    monkeypatch.setattr(engines.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(engines.importlib.metadata, "version", lambda name: "1.2.3")
    engines.note_models_blocked("docling")
    by_id = {item["id"]: item for item in engines.capabilities()}
    assert by_id["docling"]["models_blocked"] and by_id["docling"]["installed"]
    assert "refused its model download" in by_id["docling"]["note"]
    assert not by_id["paddleocr"]["models_blocked"]
    assert "download model weights" in by_id["paddleocr"]["note"]
