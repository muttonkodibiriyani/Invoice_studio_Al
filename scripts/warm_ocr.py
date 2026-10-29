"""Download public OCR models and verify all local readers during image build."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES=(
    ("invoice2data",ROOT/"samples/invoice.pdf"),
    ("paddleocr",ROOT/"samples/invoice-scan.png"),
    ("docling",ROOT/"samples/invoice-scan.png"),
)


def warm(engine,source,work):
    output=work/f"{engine}.json"
    command=[
        sys.executable,"-m","app.ocr_worker",
        "--engine",engine,
        "--file",str(source),
        "--output",str(output),
        "--templates",str(ROOT/"app/templates"),
        "--language","en",
    ]
    result=subprocess.run(command,cwd=ROOT,text=True,capture_output=True,timeout=600)
    if result.returncode or not output.exists():
        detail=result.stderr.strip()[-2000:] or "worker produced no parse output"
        if output.exists():detail=json.loads(output.read_text()).get("error","Unknown error")+": "+detail
        raise RuntimeError(f"{engine} warm-up failed: {detail}")
    try:
        payload=json.loads(output.read_text())
    except (OSError,json.JSONDecodeError) as exc:
        raise RuntimeError(f"{engine} warm-up returned unreadable JSON") from exc
    if payload.get("error"):
        raise RuntimeError(f"{engine} warm-up failed: {payload.get('hint','reader error')}")
    invoice=payload.get("invoice")
    if not payload.get("text","").strip() or not isinstance(invoice,dict):
        raise RuntimeError(f"{engine} warm-up did not produce text and a parsed invoice")
    if invoice.get("number")!="DEMO-2026-001" or len(invoice.get("lines",[]))!=2:
        raise RuntimeError(f"{engine} warm-up did not parse the expected two-line synthetic invoice")
    return {"engine":engine,"characters":len(payload["text"]),"lines":len(invoice["lines"])}


def warm_additional_paddle_models():
    # English also covers Chinese; one Latin model covers French and German.
    # Instantiate the two remaining recognition models so runtime OCR never
    # needs to download code or weights for a supported UI language.
    from app.ocr_worker import paddle_reader
    for language in ("ar", "fr"):
        reader = paddle_reader(language)
        del reader


def main():
    with tempfile.TemporaryDirectory(prefix="inv-studio-warm-") as directory:
        work=Path(directory)
        results=[warm(engine,source,work) for engine,source in CASES]
        warm_additional_paddle_models()
    print("OCR model warm-up complete: "+", ".join(f"{x['engine']} ({x['lines']} lines)" for x in results))


if __name__=="__main__":
    main()
