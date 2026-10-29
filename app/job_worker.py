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
from .models import Invoice, ProcessingOptions
from .sandbox import limit

ERRORS={"ReaderTimedOut":engines.ReaderTimedOut,"ReaderFailed":engines.ReaderFailed}


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

    def ai_reader(path,text,options):
        # The supplier examples (LearnedStore.prompt_examples) are matched against the read text here, not there.
        examples=""
        if learned is not None:
            try:examples=learned.prompt_examples(text or "")
            except Exception:examples=""
        read=parent.call("ai",text=text,examples=examples)
        return Invoice.model_validate(read["invoice"]),read["meta"]

    engines.capabilities=lambda:parent.call("capabilities")
    engines.models_blocked=lambda engine:parent.call("models_blocked",engine=engine)
    engines.note_models_blocked=lambda engine:parent.call("note_models_blocked",engine=engine)
    engines.local_read=local_read
    engines._LOCAL_OCR_SLOTS=Slot(parent,"ocr");engines._AI_SLOTS=Slot(parent,"ai")
    progress=lambda engine,message,details=None:parent.call("progress",engine=engine,message=message,details=details)
    try:
        result=engines.process(Path(start["path"]),ProcessingOptions.model_validate(start["options"]),
                               SimpleNamespace(root=root),ai_reader,progress)
        result["annotated_evidence"]=annotate(result.get("evidence"),result["invoice"],filename=start["filename"])
    except Exception as e:
        # The class name only: no document content.
        parent.send({"failed":type(e).__name__});raise SystemExit(1)
    parent.send({"done":result})


if __name__=="__main__":main()
