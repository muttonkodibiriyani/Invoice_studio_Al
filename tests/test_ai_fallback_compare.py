"""scripts/ai_fallback_compare.py against the real app in cloud mode with the reader faked, as test_live_invoice_run."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ai_fallback_compare as compare  # noqa: E402
from app import ai_rules  # noqa: E402
from tests import test_verify_deployed as verify  # noqa: E402

SAMPLES, Reader = verify.SAMPLES, verify.Reader
app, signed_in = verify.app, verify.signed_in  # fixtures: the real app in cloud mode, signed in as owner
SECRET_VALUE = "PRIVATE-ITEM-77"


def corpus(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    shutil.copy(SAMPLES / "invoice.pdf", root / "a.pdf")
    truth = json.loads((SAMPLES / "invoice.json").read_text())
    truth["lines"][0]["item_id"] = "1001"
    (root / "a.json").write_text(json.dumps(truth))
    shutil.copy(SAMPLES / "invoice-2.pdf", root / "no-truth.pdf")
    return root, truth


def connect(app, monkeypatch):
    monkeypatch.delenv("INV_STUDIO_AZURE_ENDPOINT", raising=False)
    azure = app.state.providers.azure
    azure.save(azure.prepare("https://acme.openai.azure.com", "a-key-0123456789", "reader"))


def run(client, root, tmp_path, *extra):
    out = tmp_path / "out"
    code = compare.main([str(root), "--out", str(out), "--job-timeout", "60", "--poll", "0.05", *extra], client=client)
    return code, json.loads((out / "ai-compare.json").read_text(encoding="utf-8")) if out.exists() else None


def test_no_foundry_connection_uploads_nothing(signed_in, tmp_path, monkeypatch, capsys):
    root, _ = corpus(tmp_path)
    reader = Reader(monkeypatch)
    code, report = run(signed_in, root, tmp_path)
    assert code == 2 and report is None and reader.seen == []
    assert "not connected" in capsys.readouterr().err


def test_counts_per_cell_and_per_rule_with_no_value_in_the_output(app, signed_in, tmp_path, monkeypatch, capsys):
    root, truth = corpus(tmp_path)
    connect(app, monkeypatch)
    rule_id = ai_rules.add(app.state.store, {"supplier_name": "S", "lines": [{"sku": "ABC-1"}]},
                           [{"line": 0, "kind": "item", "field": "item_id", "value": "1001", "source": "Item master",
                             "why": "same", "row_site": ""}])[0]
    fills = [{"line": 0, "rule": rule_id, "item_id": "1001", "source": "Item master"},
             {"line": 1, "rule": rule_id, "item_id": SECRET_VALUE, "source": "Item master"}]

    def read(engine):
        return json.loads((SAMPLES / "invoice.json").read_text())
    reader = Reader(monkeypatch, read=read)
    real = reader.__call__

    def with_ai(path, options, *args, **kwargs):
        result = real(path, options, *args, **kwargs)
        if options.ai_fallback:
            result["invoice"]["lines"][0]["item_id"] = "1001"
            result["invoice"]["lines"][1]["item_id"] = SECRET_VALUE
            result["readers"]["ai"].update(status="gap_fill", calls=1, filled=["date"], flagged=["net"], rules_applied=fills,
                                           suggestions=[{"line": 0, "value": SECRET_VALUE}])
        return result
    monkeypatch.setattr(verify.main_module, "process_job", with_ai)
    code, report = run(signed_in, root, tmp_path, "--redact-names", "--clean-up")
    assert code == 0
    assert [(o["ai_fallback"], o["provider"]) for o in reader.seen][1] == (True, "azure") and reader.seen[0]["ai_fallback"] is False
    assert list(report["invoices"]) == ["invoice-001"], "an invoice with no truth file is left out"
    row = report["invoices"]["invoice-001"]
    assert row["off"]["blank"] == 1 and row["on"]["blank"] == 0 and row["on"]["correct"] == row["off"]["correct"] + 1
    assert (row["filled"], row["flagged"], row["suggestions"]) == (1, 1, 1)
    assert report["rules"][rule_id]["filled"] == 2 and report["rules"][rule_id]["right"] == 1
    assert report["totals"]["both_read"] == 1
    assert SECRET_VALUE not in json.dumps(report) and SECRET_VALUE not in capsys.readouterr().out
