import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from .models import Invoice
from .evidence import build as build_evidence
from . import scan_guard

ROOT=Path(__file__).resolve().parent
def concurrency_from_env(name,default,ceiling):
    """A reader slot count from the environment: at least 1, at most `ceiling`, `default` when unset or not a number."""
    try:value=int(os.getenv(name,"") or default)
    except ValueError:value=default
    return max(1,min(ceiling,value))


# A recovery reader can coexist with its original process briefly. Keep heavy
# local OCR within the deployment's memory budget; native and AI reads continue.
# Two concurrent OCR subprocesses are useful only when a deployment has enough
# CPU as well as memory, so operators must opt in after sizing the service.
LOCAL_OCR_CONCURRENCY=concurrency_from_env("INV_LOCAL_OCR_CONCURRENCY",1,2)
_LOCAL_OCR_SLOTS=threading.BoundedSemaphore(LOCAL_OCR_CONCURRENCY)
# AI reads wait on the provider, not the CPU: several run at once, independently of the local OCR slot, so a
# large batch keeps moving while one scan is being OCR'd. Four fits the 4 vCPU / 8 GiB instance and provider limits.
AI_CONCURRENCY=concurrency_from_env("INV_AI_CONCURRENCY",4,16)
_AI_SLOTS=threading.BoundedSemaphore(AI_CONCURRENCY)

# A scan reader downloads its models the first time it runs. When a network refuses that download,
# the reader cannot read anything until the network is fixed, and every later invoice would wait for
# the same refused download and then read nothing. The reader process reports this error kind when the
# error was raised by the code that fetches the models (ocr_worker.MODEL_FETCHERS); the reader is then
# remembered as unusable for a while and skipped with a reason instead.
DOWNLOAD_REFUSED="ModelDownloadRefused"
# Long enough that a batch is not slowed by a reader that cannot work, short enough that fixing the
# network (or naming a proxy) brings the reader back without restarting the application.
try:MODELS_BLOCKED_SECONDS=max(0,int(os.getenv("INV_MODELS_BLOCKED_SECONDS","") or 900))
except ValueError:MODELS_BLOCKED_SECONDS=900
_models_blocked={}
_models_blocked_lock=threading.Lock()


class ReaderTimedOut(ValueError):
    """A reader process ran past its time limit and was stopped."""


class ReaderFailed(ValueError):
    """A reader process failed.

    The message stays the operator's hint, which never names document content. `kind` is the
    exception class the reader raised, which is what tells a refused model download from a
    document the reader could not read.
    """

    def __init__(self,hint,kind=""):
        super().__init__(hint)
        self.kind=kind or ""


def is_download_failure(kind):
    """True when the reader process says its error came from fetching its models."""
    return kind==DOWNLOAD_REFUSED


def note_models_blocked(engine):
    with _models_blocked_lock:_models_blocked[engine]=time.monotonic()


def models_blocked(engine):
    """How long ago this reader's model download was refused, or None if it is worth trying again."""
    with _models_blocked_lock:
        at=_models_blocked.get(engine)
        if at is None:return None
        waited=time.monotonic()-at
        if waited>MODELS_BLOCKED_SECONDS:
            _models_blocked.pop(engine,None)
            return None
    return waited


def forget_models_blocked(engine=None):
    """Try the download again now: after a proxy is configured, or in a test."""
    with _models_blocked_lock:
        _models_blocked.pop(engine,None) if engine else _models_blocked.clear()


def _install_hint(engine):
    """How to install a reader, on the machine the note is read on.

    The command differs per platform, and on a Mac with no PyTorch build Docling cannot be
    installed at all, so a note telling the owner to run the installer again would never go
    away. ``python`` on its own is not a command on every machine, so the venv's own
    interpreter is named.
    """
    if sys.platform=="win32":
        return ("Not installed. Run scripts\\setup.cmd --ocr, then check with "
                ".venv\\Scripts\\python scripts\\check_engines.py")
    if sys.platform=="darwin" and engine=="docling":
        return ("Not installed. Run ./scripts/setup.sh --ocr. On an Intel Mac, or macOS 13 or older, "
                "Docling cannot run because it needs PyTorch; use the cloud version for Docling.")
    return ("Not installed. Run ./scripts/setup.sh --ocr, then check with "
            ".venv/bin/python scripts/check_engines.py")


BLOCKED_NOTE=("Installed, but the network refused its model download, so it is being skipped. "
              "Invoices are read by the other readers. It is tried again on its own after a while, "
              "or straight away once a proxy is set.")


def capabilities():
    result=[]
    for engine,module,package in [("invoice2data","invoice2data","invoice2data"),("paddleocr","paddleocr","paddleocr"),("docling","docling","docling")]:
        installed=importlib.util.find_spec(module) is not None
        if engine=="paddleocr": installed=installed and importlib.util.find_spec("paddle") is not None
        blocked=installed and models_blocked(engine) is not None
        result.append({"id":engine,"installed":installed,"models_blocked":bool(blocked),
            "version":importlib.metadata.version(package) if installed else None,
            "note":BLOCKED_NOTE if blocked else
                "Installed; first use may download model weights" if installed and engine!="invoice2data" else
                "Ready" if installed else _install_hint(engine)})
    return result


def quality(i:Invoice | None):
    if i is None:return 0,["No structured invoice fields were returned"]
    missing=[f for f in ("number","date","currency","net","tax") if getattr(i,f) in (None,"")]
    if not i.lines:missing.append("line items")
    for n,l in enumerate(i.lines,1):
        for f in ("qty","price","uom"):
            if getattr(l,f) in (None,""):missing.append(f"line {n} {f}")
        if not l.sku and not l.gtin:missing.append(f"line {n} item identity")
    try:date.fromisoformat(i.date or "")
    except ValueError:
        if "date" not in missing:missing.append("unambiguous invoice date")
    if i.lines and i.net is not None and all(l.qty is not None and l.price is not None for l in i.lines):
        if all(getattr(l,"net_amount",None) is not None for l in i.lines):
            net=sum((l.net_amount for l in i.lines),Decimal(0))
        else:net=sum((l.qty*l.price for l in i.lines),Decimal(0))
        if abs(net-i.net)>rounding_tolerance(len(i.lines)):missing.append("line amounts do not reconcile with net total")
    total=6+4*len(i.lines)
    return min(0.99 if missing else 1.0,round(max(0,1-len(missing)/total),2)),missing


def rounding_tolerance(count:int):
    """How far a sum of printed line amounts may sit from the printed total.

    Each line amount is rounded to the minor unit on its own, so a page of them can differ
    from the total it is printed under by a minor unit a line. The allowance is capped so a
    long invoice cannot drift far enough to hide a misread figure.
    """
    return Decimal("0.01")*max(1,min(count,25))


def _native_reconciliation(i:Invoice,text):
    if i.net is None or not i.lines or not all(line.net_amount is not None for line in i.lines):
        return None
    printed_total=sum((line.net_amount for line in i.lines),Decimal(0))
    if abs(printed_total-i.net)<=rounding_tolerance(len(i.lines)):
        return "printed_line_total"
    matches=re.findall(
        r"(?im)^\s*(?:discount|less(?:\s+discount)?)\s*(?:[:\-]\s*)?"
        r"(?:[A-Z]{3}\s*)?(\(?-?[0-9][0-9,]*(?:\.[0-9]{1,3})?\)?)\s*$",
        text,
    )
    if len(matches)!=1:return None
    visible=matches[0]
    negative=visible.startswith("-") or (visible.startswith("(") and visible.endswith(")"))
    try:discount=Decimal(visible.strip("()-").replace(",",""))
    except InvalidOperation:return None
    if negative:return None
    if abs(printed_total-discount-i.net)<=rounding_tolerance(len(i.lines)):
        return "explicit_document_discount"
    return None


def native_pdf_quality(i:Invoice | None,text=""):
    """Require internally checkable facts before an embedded-text read skips OCR."""
    if i is None:return False,["no structured invoice"]
    issues=[]
    for field in ("number","currency","net","tax"):
        if getattr(i,field) in (None,""):issues.append(field)
    if not i.lines:issues.append("line items")
    for n,line in enumerate(i.lines,1):
        if not (line.sku or line.gtin):issues.append(f"line {n} item identity")
        for field in ("qty","uom","price","net_amount"):
            if getattr(line,field) in (None,""):issues.append(f"line {n} {field}")
    if i.net is not None and i.lines and all(line.net_amount is not None for line in i.lines):
        if _native_reconciliation(i,text) is None:
            issues.append("printed line amounts do not reconcile with net total or one explicit document discount")
    identityless=[x for x in issues if x.endswith(" item identity")]
    if identityless and len(identityless)<len(i.lines) and len(identityless)==len(issues) and _arithmetic_complete(i):
        # The line prints no code: keep the printed read with the code blank, flagged for review.
        return True,[f"{x.removesuffix(' item identity')} prints no item code; kept blank for review"
                     for x in identityless]
    return not issues,issues


def _arithmetic_complete(i:Invoice):
    """Every printed amount checks: qty x price per line, lines to net, line tax to tax."""
    cent=Decimal("0.01")
    for line in i.lines:
        if line.tax_amount is None:return False
        if (line.qty*line.price).quantize(cent,ROUND_HALF_UP)!=line.net_amount:return False
    return (sum((line.net_amount for line in i.lines),Decimal(0))==i.net
            and sum((line.tax_amount for line in i.lines),Decimal(0))==i.tax)


def reader_env():
    # Workers receive runtime paths, not the application's API keys or provider tokens.
    allowed_env={"PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","SSL_CERT_FILE","SSL_CERT_DIR",
                 "REQUESTS_CA_BUNDLE","TMPDIR","TMP","TEMP","XDG_CACHE_HOME","HF_HOME",
                 "HF_HUB_OFFLINE","HF_HUB_DISABLE_TELEMETRY","DO_NOT_TRACK","PADDLE_PDX_CACHE_HOME",
                 # Windows resolves the interpreter, the user profile and the model cache
                 # through these. A reader started without them cannot start at all, so on
                 # Windows every engine reports itself unavailable.
                 "SYSTEMROOT","SYSTEMDRIVE","WINDIR","USERPROFILE","HOMEDRIVE","HOMEPATH",
                 "APPDATA","LOCALAPPDATA","PROGRAMDATA","COMSPEC","PATHEXT","NUMBER_OF_PROCESSORS",
                 "PROCESSOR_ARCHITECTURE",
                 # A company network or VPN often reaches the model hosts only through a proxy, and
                 # may serve its own TLS certificate. Without these the first run cannot download the
                 # models at all, and the only symptom is a refused connection. The lower-case names
                 # are the convention on macOS and Linux, the upper-case ones on Windows.
                 "HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY",
                 "http_proxy","https_proxy","all_proxy","no_proxy",
                 "HF_ENDPOINT","CURL_CA_BUNDLE"}
    env={k:v for k,v in os.environ.items() if k in allowed_env}
    env["OMP_NUM_THREADS"]="2"
    # The reader prints its diagnostics as UTF-8 whatever locale started the app, so they are
    # decoded as UTF-8 here rather than in the parent's code page.
    env["PYTHONUTF8"]="1"
    return env


def local_read(engine,path,root,language="en",extra=()):
    output=root/"work"/(uuid.uuid4().hex+".json")
    timeout=int(os.getenv("INV_ENGINE_TIMEOUT","360" if engine=="paddleocr" else "240"))
    cmd=[sys.executable,"-m","app.ocr_worker","--engine",engine,"--file",str(path),"--output",str(output),
         "--templates",str(ROOT/"templates"),"--templates",str(root/"templates"),"--language",language,
         "--budget-seconds",str(timeout)]
    if os.getenv("INV_STUDIO_LEARN","1")!="0":
        # Learned supplier templates live in the private data directory (app.learned), never in the repo.
        cmd+=["--learned-templates",str(root/"learned"/"templates")]
    # Threads count towards CPU time, so the cap sits above what four busy threads can use within the wall-clock
    # timeout, which stays the main bound. torch and Paddle reserve far more address space than they use, so an
    # address-space cap is opt-in (INV_STUDIO_READER_MAX_AS_MB).
    cmd+=["--cpu-seconds",str(max(300,4*timeout)),"--max-as-mb",os.getenv("INV_STUDIO_READER_MAX_AS_MB","0")]
    cmd+=list(extra)
    env=reader_env()
    try:
        proc=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",errors="replace",
                            timeout=timeout,env=env)
        if not output.exists():raise ReaderFailed("Reader process failed before producing output")
        result=json.loads(output.read_text(encoding="utf-8"))
        if proc.returncode or result.get("error"):
            raise ReaderFailed(result.get("hint","Reader failed"),result.get("error"))
        return result
    except subprocess.TimeoutExpired:
        raise ReaderTimedOut("Reader timed out. Try another engine or inspect this document manually.") from None
    finally:output.unlink(missing_ok=True)


def _work_file_read(data,suffix,extra,root):
    """local_read over data written to a private (0o600) random work file, removed afterwards."""
    path=root/"work"/(uuid.uuid4().hex+suffix)
    path.touch(0o600)
    try:
        path.write_bytes(data)
        return local_read("invoice2data",path,root,extra=extra)
    finally:path.unlink(missing_ok=True)


def split_check(content,root):
    """multi_invoice.analyse_pdf over an uploaded PDF, in a reader child under the readers' time limit.

    The API process never parses the upload or runs the page-text patterns itself; it gets back the plain outcome.
    """
    return _work_file_read(content,".pdf",("--split-check",),root)["split"]


def stored_learn(text,boxes,tables,invoice,source_id,root):
    """Learn from an exported invoice in a reader child under the readers' time limit: the parsers' read of the
    stored text (the baseline), then the supplier template learned from it and checked against earlier invoices.

    The API process never runs reader or learning patterns over this text itself: a pattern slow on some text
    stops that child, not every request. Returns the learning outcome and the learned files for the caller to write.
    """
    stored=json.dumps({"text":text,"boxes":boxes,"tables":tables,
                       "learn":{"invoice":invoice,"source_id":source_id,"data_root":str(root)}},default=str)
    return _work_file_read(stored.encode("utf-8"),".stored",("--stored",),root)["learned_record"]


def text_checks(text,root):
    """The review's printed-text checks (target_check.text_checks) over stored text, in a reader child under the
    readers' time limit: the API process stores what comes back and never runs these patterns over the text."""
    return _work_file_read(json.dumps({"text":text}).encode("utf-8"),".stored",("--text-checks",),root)["text_checks"]


def verify_evidence(invoice,evidence,boxes,root):
    """evidence.verify_with_ocr over an AI read and a local OCR layer, in a reader child under the readers' time limit."""
    data=json.dumps({"invoice":invoice,"evidence":evidence,"boxes":boxes},default=str)
    return _work_file_read(data.encode("utf-8"),".evidence",("--verify-evidence",),root)["evidence"]


class ChecksTimedOut(ReaderTimedOut):
    """The job child's pattern work between two reads ran past the readers' time limit and was stopped."""


# Settings process() reads itself; the job child gets these and reader_env() only, never the app's keys.
JOB_ENV=("INV_STUDIO_AI_CROSS_CHECK",)
JOB_READERS=("invoice2data","paddleocr","docling")
# The most the API process reads of one message from the job child, in MB; it holds a whole result, text and boxes
# included. A longer message stops the child and fails the job. There is always a cap: blank, 0, negative or
# non-numeric means the default.
def _job_message_mb(value,default=64):
    try:mb=int(value or default)
    except ValueError:return default
    return mb if mb>0 else default
JOB_MESSAGE_MB=_job_message_mb(os.getenv("INV_STUDIO_JOB_MESSAGE_MB",""))


def process_job(path,options,store,ai_reader,progress=lambda *args:None,learned=None,filename=None):
    """process() in a job child, so the pattern work over a document's read text runs outside the API process.

    The child does every check between and after the reads: the native-text gates, evidence, the cross-read guards,
    the printed-column flags, the AI prompt's supplier examples and the evidence annotation. Everything with an effect
    stays here and the child asks for it one call at a time: the reader processes (local_read), the OCR and AI slots,
    the model-download memory, progress and the AI call, which holds the provider keys. Each stretch of the child's
    own work, from one answer to its next call, is held to the readers' time limit; past it the child is stopped and
    ChecksTimedOut ends the job, so a slow pattern stops one job, not the workers. The child also carries the readers'
    CPU limit. Returns process()'s result plus "annotated_evidence".
    """
    import queue
    timeout=int(os.getenv("INV_ENGINE_TIMEOUT","240"))
    env=reader_env()
    env.update({name:os.environ[name] for name in JOB_ENV if name in os.environ})
    cmd=[sys.executable,"-m","app.job_worker","--cpu-seconds",str(max(300,4*timeout)),
         "--max-as-mb",os.getenv("INV_STUDIO_READER_MAX_AS_MB","0")]
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          text=True,encoding="utf-8",errors="replace",env=env)
    # One message at a time, each read up to the cap: the child cannot grow this process's memory past that.
    lines=queue.Queue(maxsize=1);cap=JOB_MESSAGE_MB*1024*1024
    def pump():
        while True:
            line=proc.stdout.readline(cap)
            if not line or not line.endswith("\n"):
                lines.put(None if len(line)<cap else "");return
            lines.put(line)
    reader=threading.Thread(target=pump,daemon=True,name="job-child");reader.start()
    slots={"ocr":_LOCAL_OCR_SLOTS,"ai":_AI_SLOTS};held=[]
    def send(message):
        proc.stdin.write(json.dumps(message,default=str)+"\n");proc.stdin.flush()
    def answer(call,args):
        if call=="capabilities":return capabilities()
        if call=="models_blocked":return models_blocked(str(args["engine"]))
        if call=="note_models_blocked":return note_models_blocked(str(args["engine"]))
        if call=="acquire":
            # A second acquire of a held slot would wait on itself with no timer running: refused instead.
            if args["slot"] in held:raise ValueError("The slot is already held")
            slots[args["slot"]].acquire();held.append(args["slot"]);return None
        if call=="release":
            held.remove(args["slot"]);slots[args["slot"]].release();return None
        if call=="progress":return progress(str(args["engine"]),str(args["message"]),args.get("details"))
        if call=="local_read":
            if args["engine"] not in JOB_READERS:raise ValueError("Unknown reader")
            return local_read(args["engine"],path,store.root,options.language)
        if call=="ai":
            candidate,meta=ai_reader(path,str(args["text"]),options,examples=str(args["examples"]))
            return {"invoice":candidate.model_dump(mode="json"),"meta":meta}
        raise ValueError("Unknown job call")
    try:
        send({"path":str(path),"root":str(store.root),"options":options.model_dump(mode="json"),
              "filename":filename,"learned":learned is not None})
        while True:
            try:line=lines.get(timeout=timeout)
            except queue.Empty:
                raise ChecksTimedOut("The invoice checks timed out. Try another engine or inspect this document manually.") from None
            if line is None:raise ReaderFailed("The invoice checks stopped before finishing")
            if not line:raise ReaderFailed("The invoice checks sent a message over the size limit")
            message=json.loads(line)
            if "done" in message:return message["done"]
            if "failed" in message:raise ReaderFailed("The invoice checks failed",message["failed"])
            try:reply={"ok":answer(message["call"],message.get("args") or {})}
            except Exception as e:
                reply={"raise":{"type":type(e).__name__,"message":str(e),"kind":getattr(e,"kind","") or ""}}
            send(reply)
    finally:
        if proc.poll() is None:proc.kill()
        proc.wait()
        for name in held:slots[name].release()
        # The reader may be waiting to hand over a last message; take it so the thread ends with the child.
        while reader.is_alive():
            try:lines.get(timeout=0.1)
            except queue.Empty:pass


def verify_scan(path,root,language="en"):
    """A local OCR pass over a scan the AI read alone: the text layer and word boxes only, as evidence.

    It runs under the same local OCR slots as any reader and never produces invoice values.
    """
    with _LOCAL_OCR_SLOTS:
        result=local_read("paddleocr",path,root,language,extra=("--text-only",))
    return result.get("boxes") or [],result.get("text") or ""


def needs_scan_evidence(result,options):
    """True when the AI read a scan alone: no usable text layer, so its evidence has no boxes yet."""
    return (str(result.get("selected_engine","")).startswith(f"{options.provider} /")
            and len(str(result.get("text") or "").strip())<40 and not result.get("boxes"))


# Fields a reader is recorded for in job.readers (internal enrichment fields are set later by the rules).
READER_HEADER_FIELDS=("number","supplier_name","buyer_name","po","date_printed","date","currency","net","tax")
READER_LINE_FIELDS=("sku","gtin","description","qty","uom","price","net_amount","tax_amount")
AMOUNT_FIELDS={"net","tax","qty","price","net_amount","tax_amount"}
AI_DISAGREEMENT="The AI read a different value here"
# The AI boundary (docs/AI_BOUNDARY.md). After the local readers, process() asks the AI only for:
#   fallback    the local result fails _basic_checks; the AI fills empty fields, its lines stand in only for
#               local lines that do not sum to the net, and on a weak scan read its self-checked read replaces
#               only the fields that are empty or disagree (whole lines only when they do not align one to one);
#   gap_fill    the local result passes the checks but leaves gaps; the AI fills empty fields only;
#   cross_check a complete local result, only when ai_cross_check or INV_STUDIO_AI_CROSS_CHECK=1; flags only.
# With ai_fallback off none of these runs. engine="ai" is the one other path: the user chose the AI as the reader.
# Every field's reader is in readers.header/lines (an engine id or "ai"); readers.ai.reason says why the AI ran.
AI_REASONS={
    "fallback":"The local readers' result failed the basic checks (no lines, no number or net, or totals that do not reconcile); the AI filled empty fields, its lines stand in only for local lines that do not sum to the net, and every disagreement is flagged",
    "gap_fill":"The local readers left gaps; the AI filled only empty fields and every disagreement is flagged",
    "cross_check":"The local readers read a complete invoice; the AI cross-check flags disagreements and fills optional empty fields",
    "skipped":"The local readers read a complete, reconciled invoice; the AI cross-check is off",
    "whole":"No local reader read this invoice, so the AI read the whole invoice; review every field against the source",
    "off":"AI fallback is off","unavailable":"Select a model in Engines & AI",
    "selected":"The AI was the selected reader and read the whole invoice; review every field against the source",
    "selected_kept":"The AI was the selected reader, but a local reader read as much, so the local read is kept"}


UNRECONCILED="line amounts do not reconcile"


def _printed_uom(invoice):
    """Whether the page prints a unit of measure at all: one line carrying one is enough."""
    return any((line.uom or "")!="" for line in (invoice.lines if invoice is not None else []))


def _open_gaps(invoice,gaps):
    """Quality gaps less the ones that are not short reads: an unreconciled-lines gap when quantity x
    unit price sums to the net (the target check's basis), so a line-amount column read off by a
    discount or tax column does not count; and an empty unit of measure on a page that prints no unit
    of measure anywhere, which is an absent field rather than something to send a paid AI call after."""
    if not _printed_uom(invoice):gaps=[x for x in gaps if not x.endswith(" uom")]
    if invoice is None or invoice.net is None or not invoice.lines:return gaps
    if any(l.qty is None or l.price is None for l in invoice.lines):return gaps
    # Within one unit of the net's printed precision (at least cents), so a three-decimal net must match to 0.001.
    unit=Decimal(1).scaleb(-max(2,-invoice.net.as_tuple().exponent))
    if abs(sum((l.qty*l.price for l in invoice.lines),Decimal(0))-invoice.net)>unit:return gaps
    return [x for x in gaps if not x.startswith(UNRECONCILED)]


def _basic_checks(invoice,missing):
    """The engines' result is usable when it names the invoice, has lines with a net total, and reconciles."""
    if invoice is None or not invoice.lines:return False
    if invoice.number in (None,"") or invoice.net is None:return False
    return not any(x.startswith(UNRECONCILED) for x in missing)


def _same(field,mine,theirs):
    """Two readers agree when amounts are numerically equal or text matches by digits and letters."""
    if field in AMOUNT_FIELDS:
        try:return Decimal(str(mine))==Decimal(str(theirs))
        except (InvalidOperation,ValueError):return False
    fold=lambda v:re.sub(r"[^0-9a-z]","",str(v).casefold())
    return fold(mine)==fold(theirs)


def _aligned(mine,theirs):
    """Two line reads describe the same printed row: same code, or same quantity and price, or same text."""
    for field in ("sku","gtin","description"):
        if mine.get(field) not in (None,"") and theirs.get(field) not in (None,"") and _same(field,mine[field],theirs[field]):
            return True
    return all(mine.get(f) not in (None,"") and theirs.get(f) not in (None,"") and _same(f,mine[f],theirs[f]) for f in ("qty","price"))


def _disagreement(entry,source,other,other_entry):
    """Flag a reader's evidence entry with the AI's differing value and its evidence; the value stays."""
    entry=dict(entry) if entry else {"quote":"","page":None,"source":source}
    review={"reason":AI_DISAGREEMENT,"other_value":None if other is None else str(other)[:300]}
    if other_entry:
        review["other_quote"]=str(other_entry.get("quote") or "")[:300];review["other_page"]=other_entry.get("page")
    entry.setdefault("review",review)
    return entry


def _reader_map(invoice,source):
    """{field: reader} for every filled header field, and one map per line."""
    return ({f:source for f in READER_HEADER_FIELDS if invoice.get(f) not in (None,"")},
            [{f:source for f in READER_LINE_FIELDS if line.get(f) not in (None,"")} for line in invoice.get("lines") or []])


def _lines_reconcile(lines,net):
    """Every line has a net amount and they sum to the invoice net, bar printed rounding."""
    try:
        amounts=[Decimal(str(line.get("net_amount"))) for line in lines if line.get("net_amount") not in (None,"")]
        return (net not in (None,"") and len(amounts)==len(lines)
                and abs(sum(amounts,Decimal(0))-Decimal(str(net)))<=rounding_tolerance(len(lines)))
    except (InvalidOperation,ValueError):return False


def _engine_lines_open(engine):
    """The engines read no lines, or lines that do not reconcile with the net they read."""
    if not engine.get("lines"):return True
    try:invoice=Invoice.model_validate(engine)
    except ValueError:return True
    return any(x.startswith(UNRECONCILED) for x in _open_gaps(invoice,quality(invoice)[1]))


def merge_ai_fields(engine,ai,engine_evidence,ai_evidence,source,mode,reader=None):
    """Fill the engines' empty fields from the AI read and flag every disagreement; never overwrite a value.

    ``engine`` and ``ai`` are invoice dicts; ``reader`` names the local engine in the reader maps (default ``source``). Lines merge pairwise when both reads have the same count and the
    pair aligns; in ``fallback``, when the engines' lines do not sum to their net, the AI's lines stand in if the
    counts differ and the AI's lines sum to the engines' net; otherwise the local lines are kept. Returns (invoice, evidence, header readers, line readers, notes).
    """
    merged=dict(engine)
    evidence={"header":dict((engine_evidence or {}).get("header") or {}),"lines":[dict(x) for x in ((engine_evidence or {}).get("lines") or [])]}
    ai_header=(ai_evidence or {}).get("header") or {};ai_lines_ev=(ai_evidence or {}).get("lines") or []
    header_readers,line_readers=_reader_map(engine,reader or source);notes=[]
    for field in READER_HEADER_FIELDS:
        mine,theirs=engine.get(field),ai.get(field)
        if theirs in (None,""):continue
        if mine in (None,""):
            merged[field]=theirs;header_readers[field]="ai"
            if ai_header.get(field):evidence["header"][field]=ai_header[field]
        elif not _same(field,mine,theirs):
            evidence["header"][field]=_disagreement(evidence["header"].get(field),source,theirs,ai_header.get(field))
    engine_lines=list(engine.get("lines") or []);ai_lines=list(ai.get("lines") or [])
    while len(evidence["lines"])<len(engine_lines):evidence["lines"].append({})
    # The AI lines stand in only for local lines that do not reconcile (a fallback for a missing number keeps
    # reconciled lines), and only against a printed net: the engines' own, or - when the engines read no total
    # at all - the one the AI quotes from the document. Never a net this merge filled in from the AI lines.
    printed_net=engine.get("net")
    if printed_net in (None,"") and mode=="fallback":printed_net=ai.get("net")
    # Local lines with no net read beside them are proved by nothing, so they are open too.
    unproved=engine.get("net") in (None,"")
    stand_in=(mode=="fallback" and (_engine_lines_open(engine) or unproved)
              and len(ai_lines)!=len(engine_lines) and _lines_reconcile(ai_lines,printed_net))
    if ai_lines and (not engine_lines or stand_in):
        merged["lines"]=ai_lines;evidence["lines"]=[dict(x) for x in ai_lines_ev]
        line_readers=_reader_map(ai,"ai")[1]
        notes.append(f"The AI read {len(ai_lines)} lines where the local readers read {len(engine_lines)}"
                     +("; the local lines do not sum to the net, so the AI lines are shown" if engine_lines else "")
                     +("; they sum to the printed total the AI read" if engine_lines and unproved else ""))
    elif ai_lines and len(ai_lines)!=len(engine_lines):
        notes.append(f"The AI read {len(ai_lines)} lines where the local readers read {len(engine_lines)}; the local lines are kept"
                     +("; the AI lines do not sum to the printed net either"
                       if mode=="fallback" and (_engine_lines_open(engine) or unproved) else ""))
    elif ai_lines:
        merged["lines"]=[]
        for n,(mine_line,their_line) in enumerate(zip(engine_lines,ai_lines)):
            line=dict(mine_line);their_ev=ai_lines_ev[n] if n<len(ai_lines_ev) else {}
            aligned=_aligned(mine_line,their_line)
            if not aligned:notes.append(f"Line {n+1}: the AI line does not align with the local line; nothing filled")
            for field in READER_LINE_FIELDS:
                mine,theirs=mine_line.get(field),their_line.get(field)
                if theirs in (None,""):continue
                if mine in (None,""):
                    if not aligned:continue
                    # A printed barcode that failed its check digit is never replaced by the AI's digits.
                    if field=="gtin" and mine_line.get("barcode_unchecked"):continue
                    line[field]=theirs;line_readers[n][field]="ai"
                    if their_ev.get(field):evidence["lines"][n][field]=their_ev[field]
                elif field!="description" and not _same(field,mine,theirs):
                    evidence["lines"][n][field]=_disagreement(evidence["lines"][n].get(field),source,theirs,their_ev.get(field))
            merged["lines"].append(line)
    return merged,evidence,header_readers,line_readers,notes


def _review(entry,line,source,reason,other,code=None):
    """An evidence entry carrying this reader's review reason; the value it names is the reader's, not the invoice's."""
    entry=dict(entry) if entry else {"quote":"","page":(line or {}).get("page"),"source":source}
    entry["review"]={"reason":reason,"other_value":None if other in (None,"") else str(other)[:300]}
    if code:entry["review"]["code"]=code
    return entry


def _barcode_review(entry,line,source,reason,other):
    return _review(entry,line,source,reason,other,scan_guard.BARCODE_CODES.get(reason))


def guard_lines(invoice,evidence,line_readers,source,scan,ocr_boxes):
    """Barcodes as checked digits only; on a scan an AI barcode stands only where the page OCR prints it.

    A local barcode that fails the check digit moves to barcode_unchecked; an incomplete one is cleared. An AI
    barcode is cleared when it fails the check digit or, on a scan, when the same page's OCR does not print the
    same complete digit run as often as lines claim it, or prints it only on another line's row. A cleared
    value survives only as evidence.
    """
    lines=invoice.get("lines") or []
    rows=evidence.setdefault("lines",[])
    while len(rows)<len(lines):rows.append({})
    while len(line_readers)<len(lines):line_readers.append({})
    claims=[]
    for n,line in enumerate(lines):
        printed=line.get("gtin")
        if printed in (None,""):
            # A reader that already moved a failed-check code aside still owes the review flag.
            if line.get("barcode_unchecked") and not (rows[n].get("gtin") or {}).get("review"):
                rows[n]["gtin"]=_barcode_review(rows[n].get("gtin"),line,source,scan_guard.MISPRINT,line["barcode_unchecked"])
            continue
        gtin,unchecked,reason=scan_guard.classify_barcode(printed)
        from_ai=line_readers[n].get("gtin")=="ai"
        line["gtin"]=gtin
        if not from_ai and unchecked:line["barcode_unchecked"]=unchecked
        if gtin and line.get("barcode_unchecked"):
            # Two reads of one printed code disagree: barcode_unchecked set means gtin None, flagged.
            line["gtin"]=None;line_readers[n].pop("gtin",None)
            rows[n]["gtin"]=_barcode_review(rows[n].get("gtin"),line,source,scan_guard.MISPRINT,line["barcode_unchecked"])
            continue
        if gtin is None:
            line_readers[n].pop("gtin",None)
            rows[n]["gtin"]=_barcode_review(rows[n].get("gtin"),line,"ai" if from_ai else source,
                                    scan_guard.UNREADABLE if from_ai and unchecked else reason,printed)
        elif from_ai and scan:
            claims.append((n,line.get("page"),gtin))
    if claims:
        confirmed=scan_guard.corroborate(claims,scan_guard.page_occurrences(ocr_boxes))
        for n,page,digits in claims:
            if n in confirmed and not scan_guard.on_other_row(n,page,digits,lines,ocr_boxes):continue
            reason=scan_guard.UNCONFIRMED if n not in confirmed else scan_guard.OTHER_ROW
            lines[n]["gtin"]=None;line_readers[n].pop("gtin",None)
            rows[n]["gtin"]=_barcode_review(rows[n].get("gtin"),lines[n],"ai",reason,digits)
    return invoice,evidence


# A column of printed amounts can be read one row out of place, or a neighbouring column taken
# for it. Every line then carries a plausible, wrong amount, which a reviewer reading the screen
# cannot tell from a right one. The printed totals decide: a column that sums to its total stands
# as it was read, and one that does not is checked line by line against the quantity and the unit
# price the same row prints.
AMOUNT_TOLERANCE=Decimal("0.05")
# Rates invoices print. A rate the totals imply that is none of them means the lines do not share
# one rate, and then the totals cannot say what any single line's tax should be.
VAT_RATES=(Decimal("0"),Decimal("0.05"),Decimal("0.10"),Decimal("0.12"),Decimal("0.15"),Decimal("0.18"),Decimal("0.20"))


def _amount(value):
    """A printed amount as a number, or None when it is not one."""
    if value in (None,""):return None
    try:return Decimal(str(value).replace(",",""))
    except InvalidOperation:return None


def _printed_rate(invoice):
    """The one tax rate the printed totals imply, or None when they imply none."""
    net,tax=_amount(invoice.get("net")),_amount(invoice.get("tax"))
    if not net or tax is None:return None
    rate=tax/net
    return next((x for x in VAT_RATES if abs(rate-x)<=Decimal("0.0005")),None)


def _expected_amount(field,line,rate):
    """What the row's own quantity and unit price give for this amount, or None when they cannot."""
    qty,price=_amount(line.get("qty")),_amount(line.get("price"))
    if qty is None or price is None:return None
    if field=="net_amount":return qty*price
    percent=_amount(line.get("vat_pct"))
    share=percent/100 if percent is not None else rate
    if share is None:return None
    net=_amount(line.get("net_amount"))
    return (net if net is not None else qty*price)*share


def guard_amounts(invoice,evidence,line_readers,source):
    """Clear a line amount the row's own quantity and price contradict, and say so.

    Only a column that does not sum to its printed total is examined, so a document that adds up
    is left exactly as it was read. The cleared value stays in the evidence for the review.
    """
    lines=invoice.get("lines") or []
    if not lines:return invoice,evidence
    rows=evidence.setdefault("lines",[])
    while len(rows)<len(lines):rows.append({})
    while len(line_readers)<len(lines):line_readers.append({})
    rate=_printed_rate(invoice)
    for field,printed_field,name in (("net_amount","net","line total"),("tax_amount","tax","tax amount")):
        printed=_amount(invoice.get(printed_field))
        if printed is None:continue
        values=[_amount(line.get(field)) for line in lines]
        present=[value for value in values if value is not None]
        if not present:continue
        if len(present)==len(values) and abs(sum(present,Decimal(0))-printed)<=AMOUNT_TOLERANCE:continue
        for n,line in enumerate(lines):
            value=values[n]
            if value is None:continue
            expected=_expected_amount(field,line,rate)
            if expected is None or abs(value-expected)<=AMOUNT_TOLERANCE:continue
            line[field]=None;line_readers[n].pop(field,None)
            rows[n][field]=_review(rows[n].get(field),line,source,
                                   f"the printed quantity and unit price do not give this {name}, so it needs checking",value)
    return invoice,evidence


# A printed date can be read wrong: a two-digit year or a swapped day and month puts the
# invoice years out of its place. Any reader's date is kept only inside an invoicing window.
DATE_PAST_YEARS=10
DATE_FUTURE_DAYS=366


def _date_outside_window(value):
    """A date string that is not a plausible invoice date; None when it is fine."""
    try:year,month,day=(int(x) for x in str(value).split("-"))
    except ValueError:return "is not a date in YYYY-MM-DD form"
    try:when=date(year,month,day)
    except ValueError:return "is not a date in YYYY-MM-DD form"
    today=date.today()
    if when.year<today.year-DATE_PAST_YEARS:return f"is more than {DATE_PAST_YEARS} years old"
    if when>today+timedelta(days=DATE_FUTURE_DAYS):return "is in the future"
    return None


def guard_header(invoice,evidence,header_readers,source,text):
    """PO only from a PO label, never a P.O. Box; an absent VAT is flagged; inclusive VAT lines are one flag.

    A date outside the invoicing window is not shown: it is almost always a mis-read printed order
    or a two-digit year, so it goes back for review with the value the reader produced.
    """
    header=evidence.setdefault("header",{})
    printed_date=invoice.get("date")
    if printed_date not in (None,""):
        wrong=_date_outside_window(printed_date)
        if wrong:
            invoice["date"]=None;header_readers.pop("date",None)
            header["date"]=_review(header.get("date"),None,source,f"the date read {wrong}, so it needs checking",printed_date)
    po=invoice.get("po")
    if po not in (None,"") and scan_guard.po_from_box(po,[(header.get("po") or {}).get("quote")],text):
        invoice["po"]=None;header_readers.pop("po",None)
        header["po"]=_review(header.get("po"),None,source,"a P.O. Box in an address is not a PO number",po)
    if invoice.get("tax") in (None,""):
        if not (header.get("tax") or {}).get("review"):
            header["tax"]=_review(header.get("tax"),None,source,scan_guard.missing_tax_reason(text),None)
            header["tax"]["review"]["code"]=scan_guard.missing_tax_code(text)
    else:
        count=scan_guard.inclusive_vat_lines(invoice.get("lines"))
        if count and not (header.get("tax") or {}).get("review"):
            header["tax"]=_review(header.get("tax"),None,source,f"{count} lines: {scan_guard.INCLUSIVE_VAT}",None)
    return invoice,evidence


# Header fields every invoice prints: empty means the reader missed it (D143).
ALWAYS_PRINTED=("supplier_name","number","date","net")
NOT_READ_ALWAYS="printed on every invoice, no value read"
# Line columns flagged when their printed heading was found, with a generic column name for the reason.
LINE_COLUMNS={"sku":"SKU","gtin":"Barcode","description":"Description","qty":"Quantity","price":"Price","net_amount":"Line amount"}
_PO_LABEL=re.compile(r"(?i)\b(?:customer\s+)?(?:p\.?\s*o\.?\s*(?:no\b\.?|number\b|#)|lpo\b|purchase\s+order\s*(?:no\b\.?|number\b|#|:))")


def _line_columns_of(text,tables,boxes,headings=False):
    """The line columns printed on the page. With headings (a scan with lines), the heading words in the OCR
    boxes add the columns the table route did not name (a Barcode heading the table missed, K1).
    Raises: the caller records the failure."""
    from .docling_extract import heading_columns,line_columns
    columns=line_columns(text,tables,boxes)|(heading_columns(boxes) if headings else set())
    return columns&set(LINE_COLUMNS)


def _po_label(text):
    """The first printed PO label on a line that is not a P.O. Box address, else None."""
    for line in str(text or "").splitlines():
        match=_PO_LABEL.search(line)
        if match and not scan_guard._PO_BOX.search(line):return " ".join(match.group(0).split()).rstrip(":")
    return None


def guard_printed(invoice,evidence,text,columns,source):
    """Flag a printed field the readers left empty with code not_read; runs last, on the final invoice.

    Writes only where the value is empty and never replaces a review that already carries a code.
    evidence.lines gets one entry per invoice line.
    """
    evidence=dict(evidence or {});header=dict(evidence.get("header") or {})
    def flag(entries,key,line,reason):
        entry=entries.get(key)
        if isinstance(entry,dict) and (entry.get("review") or {}).get("code"):return
        if isinstance(entry,dict) and entry.get("review"):
            entries[key]=dict(entry);entries[key]["review"]=dict(entry["review"],code=scan_guard.TAX_UNREAD_CODE)
        else:entries[key]=_review(entry,line,source,reason,None,scan_guard.TAX_UNREAD_CODE)
    for field in ALWAYS_PRINTED:
        if invoice.get(field) in (None,""):flag(header,field,None,NOT_READ_ALWAYS)
    label=_po_label(text) if invoice.get("po") in (None,"") else None
    if label and not (header.get("po") or {}).get("review"):
        flag(header,"po",None,f"'{label}' label printed, no value read")
    evidence["header"]=header
    lines=invoice.get("lines") or []
    rows=[dict(row) if isinstance(row,dict) else {} for row in (evidence.get("lines") or [])]
    rows+=[{} for _ in range(len(lines)-len(rows))]
    for n,line in enumerate(lines):
        for field,name in LINE_COLUMNS.items():
            if field not in columns or line.get(field) not in (None,""):continue
            if field=="gtin" and line.get("barcode_unchecked"):continue
            flag(rows[n],field,line,f"{name} column present, cell empty")
    evidence["lines"]=rows
    return evidence


def _absent_fields(evidence):
    """Job-level read-time facts for empty header fields: absent_fields (not printed) and each field's reason_code."""
    codes={field:entry["review"]["code"] for field,entry in ((evidence or {}).get("header") or {}).items()
           if isinstance(entry,dict) and (entry.get("review") or {}).get("code")}
    return {"absent_fields":[f for f,c in codes.items() if c==scan_guard.TAX_ABSENT_CODE],"reason_codes":codes}


def process(path,options,store,ai_reader,progress=lambda *args:None):
    trace=[];best=None;best_score=-1;text="";boxes=[];selected="none";best_evidence={};best_source="native"
    # The engine id (or "ai") the reader maps name for the selected read; best_source is its evidence source.
    best_reader="none"
    document_type_hint=None;extraction_note=None;native_review=[]
    ai_attempted=False;invoice2data_ocr=False;ai_calls=0;ai_status="off";ai_notes=[];readers_maps=None
    # A scan (no usable text layer) is told by the PDF's own text; its OCR boxes corroborate AI barcodes.
    native_chars=None;ocr_boxes=[];scan=False;ai_reason=None
    # The text and boxes of the selected local read: the target check re-checks its values against them.
    selected_page=None;selected_tables=[];page_tables=[]
    # Every local read of this document, so a required field the selected read missed can still be
    # taken from the reader that printed it.
    local_reads=[];selected_read=None;filled_fields={}
    # Readers the chain reached but that are not installed: told on the job with their install hint, and
    # named on the AI's trace entry as why it ran.
    missing_readers=[]
    cross_check=bool(getattr(options,"ai_cross_check",False)) or os.getenv("INV_STUDIO_AI_CROSS_CHECK","0")=="1"
    def call_ai(role):
        """One AI read: (candidate, its own header evidence, completeness) with a trace entry, or None."""
        nonlocal ai_attempted,ai_calls
        ai_attempted=True;ai_calls+=1
        progress(options.provider,"Waiting for an AI reader slot")
        _AI_SLOTS.acquire()
        progress(options.provider,"AI is reading the invoice")
        start=time.monotonic()
        try:
            candidate,meta=ai_reader(path,text,options)
            meta=dict(meta or {});ai_evidence=meta.pop("evidence",None) or {}
            internal_fields=("seller","site","buyer","location","origin","market","taxCode")
            ignored=[field for field in internal_fields if getattr(candidate,field) is not None]
            candidate=candidate.model_copy(update={field:None for field in internal_fields})
            candidate=candidate.model_copy(update={"lines":[line.model_copy(update={"item_id":None}) for line in candidate.lines]})
            if ignored:meta={**meta,"unverified_internal_fields_ignored":ignored}
            score,missing=quality(candidate)
            has_fields=bool(candidate.lines or any(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines"))
            trace.append({**({"readers_not_installed":list(missing_readers)} if missing_readers else {}),"engine":options.provider,"model":options.model,"status":"extracted" if has_fields else "no_fields","completeness":score,"seconds":round(time.monotonic()-start,2),"extracted_fields":sum(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines"),"line_items":len(candidate.lines),"text_characters":len(text),"method":"vision_ai","role":role,"reason":"Invoice fields returned; review against the source" if has_fields else "AI returned no invoice fields; another reader or manual entry is required","usage":meta})
            return (candidate,ai_evidence,score) if has_fields else None
        except Exception as e:
            trace.append({"engine":options.provider,"model":options.model,"status":"failed","role":role,"reason":str(e)[:240],"seconds":round(time.monotonic()-start,2)})
            return None
        finally:
            _AI_SLOTS.release()
            progress(options.provider,"AI reading finished",{"trace":list(trace),"characters":len(text)})
    def read_ai():
        """The AI as the selected reader: its whole candidate, kept when it reads strictly more than a local pass."""
        nonlocal best,best_score,selected,best_evidence,best_source,best_reader,ai_status,ai_reason,selected_page
        read=call_ai("selected")
        if read is None:
            ai_status="failed";return
        candidate,ai_evidence,score=read;ai_status="selected"
        # A local read is kept on a completeness tie; the AI must read strictly more.
        if score>best_score:
            best=candidate;best_score=score;selected=f"{options.provider} / {options.model}";best_source=best_reader="ai";selected_page=None
            best_evidence=build_evidence(candidate.model_dump(mode="json"),boxes,"ai",header=ai_evidence)
        else:ai_reason=AI_REASONS["selected_kept"]
    def merge_ai(mode):
        """The AI after the local engines: fills empty fields, flags disagreements, never overwrites.

        The one exception is a weak local read of a scan that failed the basic checks (``fallback``): the
        AI's self-checked read of the page image replaces the fields that are empty or disagree.
        """
        nonlocal best,best_score,selected,best_evidence,best_source,best_reader,ai_status,ai_notes,readers_maps,ai_reason
        read=call_ai(mode)
        if read is None:
            ai_status="failed";return
        candidate,ai_evidence,score=read
        ai_dict=candidate.model_dump(mode="json");ai_ev=build_evidence(ai_dict,boxes,"ai",header=ai_evidence)
        ai_status=mode
        local=best.model_dump(mode="json") if best is not None else None
        # Only a local read that failed the basic checks can be replaced: one that passed keeps every value it read.
        weak=scan_guard.weak_read(local) if scan and mode=="fallback" else []
        if weak:
            # A weak local read of a scan: the AI read from the page image stands in when it passes its own
            # checks; when it does not either, nothing is filled and the invoice is not read.
            failed=scan_guard.ai_self_check(ai_dict)
            if failed:
                ai_status="not_read"
                ai_reason=("Not read: the local OCR read is weak ("+"; ".join(weak)+") and the AI read fails its checks ("
                           +"; ".join(failed)+"). Nothing was filled; enter the invoice manually.")
                return
            merged=dict(local or Invoice().model_dump(mode="json"))
            header_readers=_reader_map(local,best_reader)[0] if local else {}
            evidence={"header":dict((best_evidence or {}).get("header") or {}) if local else {},"lines":[]}
            replaced=[]
            ai_header=ai_ev.get("header") or {}
            for field in READER_HEADER_FIELDS:
                mine,theirs=merged.get(field),ai_dict.get(field)
                # A money total stands in only with the quote it was read from: no derived net or tax, no 0 for unprinted.
                if field in ("net","tax") and not str((ai_header.get(field) or {}).get("quote") or "").strip():continue
                if theirs in (None,"") or (mine not in (None,"") and _same(field,mine,theirs)):continue
                merged[field]=theirs;header_readers[field]="ai";replaced.append(field)
                evidence["header"][field]={**(ai_header.get(field) or {"quote":"","page":None}),
                                           "source":"ai","origin":scan_guard.AI_PAGE_IMAGE}
            mine_lines=(local or {}).get("lines") or [];ai_lines=ai_dict.get("lines") or []
            ai_rows=ai_ev.get("lines") or []
            if mine_lines and len(mine_lines)==len(ai_lines) and all(map(_aligned,mine_lines,ai_lines)):
                # The local lines describe the same printed rows: only the line fields that are empty or
                # disagree take the AI's value, so one misread figure does not replace every line.
                local_rows=list((best_evidence or {}).get("lines") or [])
                merged["lines"]=[dict(x) for x in mine_lines];line_readers=_reader_map(local,best_reader)[1]
                evidence["lines"]=[dict(local_rows[n]) if n<len(local_rows) else {} for n in range(len(mine_lines))]
                for n,(line,theirs) in enumerate(zip(merged["lines"],ai_lines)):
                    for field in READER_LINE_FIELDS:
                        if theirs.get(field) in (None,""):continue
                        if line.get(field) not in (None,"") and (field=="description" or _same(field,line[field],theirs[field])):continue
                        line[field]=theirs[field];line_readers[n][field]="ai";replaced.append(f"line {n+1} {field}")
                        row=ai_rows[n] if n<len(ai_rows) else {}
                        evidence["lines"][n][field]={**(row.get(field) or {"quote":"","page":None}),
                                                     "source":"ai","origin":scan_guard.AI_PAGE_IMAGE}
                what=("only the fields that are empty or disagree: "+", ".join(replaced) if replaced
                      else "nothing: it agrees with the local read")
            else:
                merged["lines"]=ai_lines;line_readers=_reader_map(ai_dict,"ai")[1]
                evidence["lines"]=[{k:{**v,"origin":scan_guard.AI_PAGE_IMAGE} for k,v in row.items()} for row in ai_rows]
                selected=f"{options.provider} / {options.model}"
                what=(f"the whole invoice ({len(ai_lines)} lines and {len(replaced)} header fields)" if local is None else
                      f"all {len(ai_lines)} lines (the local read had {len(mine_lines)} that do not align with them)"
                      +(" and these header fields: "+", ".join(replaced) if replaced else ""))
            best=Invoice.model_validate(merged);best_score=quality(best)[0];best_evidence=evidence
            readers_maps=(header_readers,line_readers)
            ai_reason=(f"The local OCR read of this scan is weak ({'; '.join(weak)}); the AI read the page image, "
                       f"passed its own checks and replaced {what}")
            return
        if best is None:
            best=candidate;best_score=score;selected=f"{options.provider} / {options.model}";best_source=best_reader="ai";best_evidence=ai_ev
            ai_reason=AI_REASONS["whole"]
            return
        merged,evidence,header_readers,line_readers,notes=merge_ai_fields(best.model_dump(mode="json"),ai_dict,best_evidence,ai_ev,best_source,mode,best_reader)
        best=Invoice.model_validate(merged);best_score=quality(best)[0];best_evidence=evidence
        readers_maps=(header_readers,line_readers);ai_notes=notes
    installed={x["id"] for x in capabilities() if x["installed"]}
    chain=["invoice2data","paddleocr","docling"] if options.engine=="auto" else ([] if options.engine=="ai" else [options.engine])
    native_preflight_engine=None
    if (path.suffix.lower()==".pdf" and options.engine in ("paddleocr","docling")
            and getattr(options,"prefer_native_text",True)):
        native_preflight_engine=options.engine
        chain=["native_pdf_text",options.engine]
    if options.engine=="invoice2data" and path.suffix.lower() in (".png",".jpg",".jpeg",".tif",".tiff",".webp",".bmp"):
        invoice2data_ocr=True;chain=["paddleocr"]
        trace.append({"engine":"invoice2data","status":"needs_ocr","reason":"This image needs local OCR input. PaddleOCR will read it before invoice2data templates and structured parsing."})
    # Text/office attachments and the local Claude bridge need native/OCR text even
    # when AI is selected explicitly. AI still runs after this preparation step.
    if options.engine=="ai":
        if options.provider=="claude_local":chain=["invoice2data","paddleocr","docling"]
        elif path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):chain=["invoice2data"]
    for engine in chain:
        reader_engine="invoice2data" if engine=="native_pdf_text" else engine
        # Local engines first on every invoice: a scan goes through the local OCR readers before the AI
        # sees it. The AI stage after the chain fills gaps and flags disagreements; it never runs ahead.
        start=time.monotonic();queue_seconds=None
        progress(engine,"Checking embedded PDF text" if engine=="native_pdf_text" else "Reading document")
        if reader_engine not in installed:
            if reader_engine not in missing_readers:missing_readers.append(reader_engine)
            trace.append({"engine":engine,"status":"unavailable","reason":_install_hint(reader_engine)});continue
        if models_blocked(reader_engine) is not None:
            # Waiting for the same refused download on every invoice costs minutes each and reads
            # nothing: skip it until the window expires, and say so instead of reporting a bad read.
            trace.append({"engine":engine,"status":"models_unavailable",
                "reason":"Skipped: the network refused this reader's model download, so it cannot read "
                         "anything yet. The other readers are unaffected."})
            progress(engine,"Reader skipped",{"trace":list(trace),"characters":len(text)})
            continue
        if engine=="paddleocr" and path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):
            trace.append({"engine":engine,"status":"skipped","reason":"Digital office/text document; use native reading or Docling"});continue
        try:
            if engine in ("paddleocr","docling"):
                queue_started=time.monotonic()
                progress(engine,"Waiting for the local OCR reader")
                with _LOCAL_OCR_SLOTS:
                    queue_seconds=time.monotonic()-queue_started
                    start=time.monotonic()
                    progress(engine,"Reading document")
                    result=local_read(reader_engine,path,store.root,options.language)
            else:
                result=local_read(reader_engine,path,store.root,options.language)
            candidate=Invoice.model_validate(result["invoice"]) if result.get("invoice") else None
            # Every later use of this read takes the model's values: a field over its cap is empty (RULE 8).
            if candidate is not None:result["invoice"]=candidate.model_dump(mode="json")
            score,missing=quality(candidate)
            if len(result["text"])>len(text):text=result["text"];boxes=result.get("boxes",[]);page_tables=result.get("tables") or []
            native_suitable,native_issues=(native_pdf_quality(candidate,result["text"])
                if engine=="native_pdf_text" else (False,[]))
            native_reconciliation=(_native_reconciliation(candidate,result["text"])
                if native_suitable else None)
            if engine=="invoice2data" and options.engine=="auto" and path.suffix.lower()==".pdf":
                # The auto chain reads the PDF's own text here; the same gate applies to an unprinted code.
                kept,notes=native_pdf_quality(candidate,result["text"])
                if kept and notes:
                    native_review=notes;missing=[x for x in missing if not x.endswith(" item identity")]
            reason=("Embedded PDF text has complete line facts reconciled through the document's explicit discount"
                if native_reconciliation=="explicit_document_discount" else
                "Embedded PDF text passes every arithmetic check; "+"; ".join(native_issues)
                if native_suitable and native_issues else
                "Embedded PDF text has complete, reconciled line facts"
                if native_suitable else "; ".join(native_issues)
                if engine=="native_pdf_text" else
                "Embedded PDF text passes every arithmetic check; "+"; ".join(native_review)
                if engine=="invoice2data" and native_review else
                "Required extraction fields present" if not missing else "; ".join(missing))
            trace_entry={"engine":engine,"method":result.get("extraction_method","template" if candidate else "text_only"),"status":"extracted" if candidate else "text_only","seconds":round(time.monotonic()-start,2),"completeness":score,"extracted_fields":sum(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines") if candidate else 0,"line_items":len(candidate.lines) if candidate else 0,"text_characters":len(result["text"]),"table_count":len(result.get("tables",[])),"parser_error":result.get("parser_error"),"reason":reason}
            if queue_seconds is not None:trace_entry["queue_seconds"]=round(queue_seconds,2)
            if engine in ("paddleocr","docling"):
                trace_entry["ocr_status"]="ok";trace_entry["tokens_per_page"]=scan_guard.tokens_per_page(result.get("boxes"))
                if not ocr_boxes and result.get("boxes"):ocr_boxes=result["boxes"]
            elif engine in ("invoice2data","native_pdf_text") and not invoice2data_ocr and native_chars is None:
                native_chars=len(str(result.get("text") or "").strip())
            trace.append(trace_entry)
            if result.get("recovery"):
                trace[-1]["recovery"]=result["recovery"]
            if candidate is not None:
                local_reads.append({"engine":engine,"invoice":result["invoice"],
                                    "boxes":result.get("boxes",[]),
                                    "source":"ocr" if engine=="paddleocr" else "native"})
            if candidate is not None and (score>best_score or (
                    score==best_score and engine==native_preflight_engine)):
                selected_read=len(local_reads)-1
                best=candidate;best_score=score
                selected=("native PDF text" if engine=="native_pdf_text" else
                    "invoice2data + PaddleOCR" if invoice2data_ocr and engine=="paddleocr" else engine)
                best_source="ocr" if engine=="paddleocr" else "native";best_reader=engine
                best_evidence=build_evidence(result["invoice"],result.get("boxes",[]),best_source)
                selected_page=(result["text"],result.get("boxes",[]))
                selected_tables=result.get("tables") or []
            progress(engine,"Text reading finished",{"trace":list(trace),"characters":len(text)})
            if engine=="native_pdf_text":
                if native_suitable:
                    native_review=native_issues
                    trace.append({"engine":native_preflight_engine,"status":"skipped",
                        "reason":"Embedded PDF text passed the structured quality checks; the selected OCR reader did not run. Disable native-text preference to force OCR."})
                    break
                continue
            if options.engine=="invoice2data" and engine=="invoice2data" and path.suffix.lower()==".pdf" and len(result["text"].strip())<40 and not (candidate and candidate.lines):
                invoice2data_ocr=True;chain.append("paddleocr")
                trace.append({"engine":"invoice2data","status":"needs_ocr","reason":"This PDF has no usable text layer. PaddleOCR will provide local OCR input for invoice2data templates and structured parsing."})
            # Native text already exists and invoice2data read a sound invoice from it: another OCR pass cannot
            # supply facts absent from the document. With no read, or one that fails the basic checks (no
            # template matched and the template-free parsers read short), the layout and OCR readers still run
            # before the AI is asked, and the score keeps the better read.
            if options.engine in ("auto","ai") and engine=="invoice2data" and len(text.strip())>=250:
                po_heading=re.search(r"(?im)^\s*(?:#{1,6}\s*)?Purchase\s+Order\s*$",text)
                po_number=re.search(r"(?i)Store\s+Purchase\s+Order\s*(?:#|No|Number)",text)
                invoice_heading=re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:(?:tax|commercial|sales|supplier)\s+)?Invoice\b",text)
                if po_heading and po_number and not invoice_heading:
                    document_type_hint="possible_purchase_order"
                    extraction_note="This document is labelled Purchase Order. Check the document type before entering invoice fields; a PO does not prove an invoice or receipt."
                elif candidate is None:
                    extraction_note="Document text was read, but no supplier invoice template matched. Use a connected AI reader or enter the fields manually."
                elif missing:
                    extraction_note="Invoice fields were read from the PDF text. Review the remaining missing fields or use AI; another OCR pass cannot supply facts absent from the document."
                if document_type_hint is None and not _basic_checks(candidate,_open_gaps(candidate,missing)):continue
                for remaining in chain[chain.index(engine)+1:]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"Native text is already readable; a supplier mapping or selected AI is needed, not another OCR pass."})
                break
            # A read short of the basic checks (no net, or lines that do not sum to it: a table read short of
            # the printed rows) is not a stopping point: the next local reader still runs and the existing
            # score keeps the better read.
            if options.engine in ("auto","ai") and engine=="paddleocr" and _basic_checks(candidate,_open_gaps(candidate,missing)):
                for remaining in chain[chain.index(engine)+1:]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"Invoice fields were read. Remaining exceptions go to the selected AI or manual review."})
                break
            if not missing:break
        except Exception as e:
            could_not_download=is_download_failure(getattr(e,"kind",""))
            if could_not_download:
                # Its own download, not this document: remember it so the rest of the batch does not
                # wait for the same refused download, and name the real cause in the trace.
                note_models_blocked(reader_engine)
            reason=("This reader could not download its models: the network refused the connection. "
                    "Nothing is wrong with this document, and it is read by the other readers."
                    if could_not_download else str(e)[:240])
            trace.append({"engine":engine,"status":"models_unavailable" if could_not_download else "failed",
                "reason":reason,"seconds":round(time.monotonic()-start,2),
                "queue_seconds":round(queue_seconds,2) if queue_seconds is not None else None})
            if engine in ("paddleocr","docling"):
                trace[-1]["ocr_status"]=("models_unavailable" if could_not_download else
                                         "timed_out" if "timed out" in str(e) else "failed")
            progress(engine,"Reader attempt finished",{"trace":list(trace),"characters":len(text)})
    def gaps_of(invoice):
        _,gaps=quality(invoice)
        if native_review and selected in ("native PDF text","invoice2data"):
            # The gate already checked every amount; an unprinted code is for review, not for the AI to supply.
            gaps=[x for x in gaps if not x.endswith(" item identity")]
        return _open_gaps(invoice,gaps)
    def fill_from_other_reads(invoice):
        """Required header fields the selected read left empty, taken from another reader's read.

        An invoice without its number or its date cannot be filed, and a second local reader often
        printed exactly the field the winning one missed. Only empty fields are filled, the reader
        that supplied each one is recorded, and nothing a reader did read is overwritten.
        """
        filled={}
        for field in ALWAYS_PRINTED+("date_printed","currency","po"):
            if invoice.get(field) not in (None,""):continue
            for index,read in enumerate(local_reads):
                if index==selected_read:continue
                value=(read["invoice"] or {}).get(field)
                if value in (None,""):continue
                invoice[field]=value;filled[field]=read
                break
        return filled
    scan=scan_guard.is_scan(native_chars,path)
    if best is not None:
        # The local read's barcodes are checked before the AI sees the gaps: a misprint is not a gap to fill.
        local=best.model_dump(mode="json");local_readers=_reader_map(local,best_source)[1]
        filled_fields=fill_from_other_reads(local)
        for field,read in filled_fields.items():
            entry=(build_evidence(read["invoice"],read["boxes"],read["source"]).get("header") or {}).get(field)
            if entry:best_evidence.setdefault("header",{})[field]=entry
        if filled_fields:
            trace.append({"engine":"readers","status":"filled","reason":"Fields the selected reader left empty, read by another local reader: "
                          +", ".join(f"{field} ({read['engine']})" for field,read in filled_fields.items())})
        local,best_evidence=guard_lines(local,dict(best_evidence or {}),local_readers,best_source,scan,ocr_boxes)
        local,best_evidence=guard_amounts(local,best_evidence,local_readers,best_source)
        best=Invoice.model_validate(local);best_score=quality(best)[0]
    missing=gaps_of(best);ai_reason=None
    # What the engines' result asks of the AI, recorded whether or not the AI runs, so AI calls per invoice
    # can be counted on an AI-off run: fallback when the engines could not read, gap fill when they left gaps,
    # cross-check (an extra call on a complete invoice) only when the owner switched it on.
    need=None if document_type_hint=="possible_purchase_order" else (
        "fallback" if not _basic_checks(best,missing) else "gap_fill" if missing else "cross_check")
    if document_type_hint=="possible_purchase_order":
        trace.append({"engine":options.provider,"status":"skipped","reason":"The document is labelled Purchase Order; invoice extraction needs the supplier invoice."})
        ai_status,ai_reason="skipped",trace[-1]["reason"]
    elif options.engine=="ai":
        if not options.model:
            trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in Engines & AI"});ai_status="unavailable"
        elif not ai_attempted:read_ai()
    elif options.ai_fallback:
        if not options.model:
            if missing:trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in Engines & AI"})
            ai_status="unavailable" if missing else "skipped"
            if not missing:ai_reason="The local readers read a complete, reconciled invoice; no model is selected"
        elif need=="cross_check" and not cross_check:ai_status="skipped"
        else:merge_ai(need)
    if ai_status=="failed":ai_reason="The AI read failed: "+str(trace[-1].get("reason") or "no reason given")[:240]
    if ai_status=="not_read":extraction_note=ai_reason
    if best is not None:
        final=best.model_dump(mode="json")
        header_map,line_map=readers_maps or _reader_map(final,best_reader)
        for field,read in filled_fields.items():
            if final.get(field) not in (None,""):header_map[field]=read["engine"]
        final,best_evidence=guard_lines(final,dict(best_evidence or {}),line_map,best_source,scan,ocr_boxes)
        final,best_evidence=guard_amounts(final,best_evidence,line_map,best_source)
        final,best_evidence=guard_header(final,best_evidence,header_map,best_source,text)
        best=Invoice.model_validate(final);best_score=quality(best)[0];readers_maps=(header_map,line_map)
    ai_record={"status":ai_status,"reason":ai_reason or AI_REASONS.get(ai_status,""),"calls":ai_calls,"need":need}
    if missing_readers and need in ("fallback","gap_fill"):
        # A reader this file needed is missing: that, not the document, is why the AI is asked.
        hint=_install_hint(missing_readers[0])
        ai_record["reason"]=", ".join(missing_readers)+": "+hint[0].lower()+hint[1:]+". "+ai_record["reason"]
    if ai_notes:ai_record["notes"]=ai_notes
    invoice=(best or Invoice()).model_dump(mode="json")
    header_readers,line_readers=readers_maps or _reader_map(invoice,best_reader)
    readers={"ai":ai_record,"header":header_readers,"lines":line_readers,"gaps":gaps_of(best),
             "not_installed":[{"engine":e,"hint":_install_hint(e)} for e in missing_readers]}
    if best is not None and best.lines and best.number and document_type_hint is None and ai_status!="not_read":
        extraction_note=None
    if native_review and selected in ("native PDF text","invoice2data"):
        extraction_note="Read from the PDF text; every printed amount checks. Review: "+"; ".join(native_review)+"."
    if selected_page is not None:
        # A later reader that lost on score (e.g. Docling after an unreconciled PaddleOCR read) must not
        # replace the selected reader's text and boxes.
        text,boxes=selected_page
    if best is not None:
        tables=selected_tables if selected_page is not None else page_tables
        try:columns=_line_columns_of(text,tables,boxes or ocr_boxes,headings=scan and bool(invoice.get("lines")))
        except Exception as e:
            # Line flags could not be placed: say so rather than leave the empty cells silent.
            columns=set();trace.append({"engine":"guard_printed","status":"columns_failed","reason":type(e).__name__})
        best_evidence=guard_printed(invoice,best_evidence,text,columns,best_source)
    return {"invoice":invoice,"text":text,"boxes":boxes,
            "trace":trace,"selected_engine":selected,"completeness":max(0,best_score),
            "document_type_hint":document_type_hint,"extraction_note":extraction_note,
            "evidence":best_evidence if best is not None else {},"readers":readers,**_absent_fields(best_evidence if best is not None else {})}
