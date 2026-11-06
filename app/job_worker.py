"""The job child: engines.process over one document, outside the API process (engines.process_job).

Every pattern check over the read text, boxes and tables runs here. What has an effect is asked of the parent one
call at a time over stdin/stdout: the reader processes, the OCR and AI slots, the model-download memory, progress and
the AI call. The child holds no keys (reader_env) and writes no files.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from . import engines
from .evidence import annotate
from .fine_rules import ocr_read
from .item_rules import RulesError
from .item_search import for_invoice
from .models import IMAGE_FORMATS, Invoice, ProcessingOptions
from .multi_invoice import page_texts_from_boxes, suspect_pages
from .sandbox import limit

ERRORS={"ReaderTimedOut":engines.ReaderTimedOut,"ReaderFailed":engines.ReaderFailed,"RulesError":RulesError}


def picture_pages(path):
    """Pages of a PDF or a TIFF that hold a picture."""
    if path.suffix.lower()==".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:return sum(1 for page in pdf.pages[:20] if page.images)
    if path.suffix.lower() in (".tif",".tiff"):
        from PIL import Image
        with Image.open(path,formats=IMAGE_FORMATS) as image:return min(getattr(image,"n_frames",1),20)
    return 1


def several_in_read(path,result):
    """Pages where a read of scanned pages shows another invoice may start; the upload check covered native text. A
    multi-page scan with no OCR text to look at (the AI read it alone) cannot be checked, so every later page is held."""
    if not ocr_read(result):return []
    texts=page_texts_from_boxes(result.get("boxes") or [])
    return suspect_pages(texts) if any(text.strip() for text in texts) else list(range(2,picture_pages(path)+1))


class Parent:
    def __init__(self,out):
        self.out=out

    def send(self,message):
        self.out.write(json.dumps(message,default=str)+"\n");self.out.flush()

    def call(self,name,**args):
        self.send({"call":name,"args":args})
        line=sys.stdin.readline()
        if not line:raise SystemExit(1)
        reply=json.loads(line)
        if "raise" in reply:
            error=reply["raise"]
            if error["type"]=="ReaderFailed":raise engines.ReaderFailed(error["message"],error["kind"])
            raise ERRORS.get(error["type"],ValueError)(error["message"])
        return reply.get("ok")


class Slot:
    """A parent slot: acquire waits for it there."""

    def __init__(self,parent,name):
        self.parent=parent;self.name=name

    def acquire(self):
        self.parent.call("acquire",slot=self.name);return True

    def release(self):
        self.parent.call("release",slot=self.name)

    def __enter__(self):
        return self.acquire()

    def __exit__(self,*exc):
        self.release()


def _searchable(invoice):
    """What the parent searches the item master and the AI-added rules with: the read's identity fields, positions kept."""
    invoice=invoice or {}
    return {"supplier_name":invoice.get("supplier_name"),"po":invoice.get("po"),
            "lines":[{f:line.get(f) for f in ("sku","gtin","barcode_unchecked","description","item_id")} for line in invoice.get("lines") or []]}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--cpu-seconds",type=int,default=0)
    p.add_argument("--max-as-mb",type=int,default=0)
    args=p.parse_args()
    if args.cpu_seconds:limit(args.cpu_seconds,args.max_as_mb or None)
    # The protocol keeps this process's own stdout; anything a library prints goes to stderr.
    out=os.fdopen(os.dup(1),"w",encoding="utf-8");os.dup2(2,1);sys.stdout=sys.stderr
    parent=Parent(out)
    start=json.loads(sys.stdin.readline())
    root=Path(start["root"])
    learned=None
    if start["learned"]:
        from .learned import LearnedStore
        learned=LearnedStore(root)

    def local_read(engine,path,root,language="en",extra=()):
        if extra:raise ValueError("A job reads with the default reader options")
        return parent.call("local_read",engine=engine)

    def ai_reader(path,text,options,local=None):
        # The supplier examples (LearnedStore.prompt_examples) are matched against the read text here, not there.
        examples=""
        if learned is not None:
            try:examples=learned.prompt_examples(text or "")
            except Exception:examples=""
        read=parent.call("ai",text=text,examples=examples,local=_searchable(local))
        return Invoice.model_validate(read["invoice"]),read["meta"]

    def ai_rule_fill(invoice):
        return parent.call("ai_rule_fill",invoice=_searchable(invoice))

    def ai_rule_keep():
        return parent.call("ai_rule_keep")

    def item_master(invoice):
        # The live item master is searched in the parent (engines.process_job): it holds the store.
        return for_invoice(lambda line,column,match:parent.call("item_search",line=line,column=column,match=match),
                           lambda:parent.call("item_sites",invoice={k:invoice.get(k) for k in ("site","supplier_name")}),
                           start.get("supplier_site") or None)

    engines.capabilities=lambda:parent.call("capabilities")
    engines.models_blocked=lambda engine:parent.call("models_blocked",engine=engine)
    engines.note_models_blocked=lambda engine:parent.call("note_models_blocked",engine=engine)
    engines.local_read=local_read
    engines._LOCAL_OCR_SLOTS=Slot(parent,"ocr");engines._AI_SLOTS=Slot(parent,"ai")
    progress=lambda engine,message,details=None:parent.call("progress",engine=engine,message=message,details=details)
    try:
        result=engines.process(Path(start["path"]),ProcessingOptions.model_validate(start["options"]),
                               SimpleNamespace(root=root),ai_reader,progress,item_master=item_master,ai_local=True,ai_rule_fill=ai_rule_fill,ai_rule_keep=ai_rule_keep)
        result["annotated_evidence"]=annotate(result.get("evidence"),result["invoice"],filename=start["filename"])
        several=several_in_read(Path(start["path"]),result)
        if several:result["several_read"]=several
    except Exception as e:
        # The class name only: no document content.
        parent.send({"failed":type(e).__name__});raise SystemExit(1)
    parent.send({"done":result})


if __name__=="__main__":main()
