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
from .models import Invoice, uom_code
from .evidence import build as build_evidence
from . import ai_context, ai_rules, item_rules, item_search, matching, rules_first, scan_guard
from .fine_rules_source import LookupRulesSource
from .numbers import to_decimal

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
# The upload's multi-invoice check (split_check) runs on a request thread, before the upload is queued. Its own
# limit is far below the readers': a 20-page invoice checks in about 5 s, while a PDF whose page draws itself 2^40
# times never finishes. At most this many run at once and one account holds all but one of them, so hostile
# uploads cannot hold every request thread or CPU (attack pack F1). An upload over the cap is answered 429 at once.
SPLIT_CHECK_CONCURRENCY=concurrency_from_env("INV_SPLIT_CHECK_CONCURRENCY",4,16)
_split_checks={}  # account -> split checks running for it
_split_checks_lock=threading.Lock()

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


class ChecksBusy(Exception):
    """Every split check slot this account may use is taken; the upload is answered 429, never queued unchecked."""


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
    discount=to_decimal(visible)
    if discount is None or discount<0:return None
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


def local_read(engine,path,root,language="en",extra=(),timeout=None):
    output=root/"work"/(uuid.uuid4().hex+".json")
    timeout=timeout or int(os.getenv("INV_ENGINE_TIMEOUT","360" if engine=="paddleocr" else "240"))
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


def _work_file_read(data,suffix,extra,root,timeout=None):
    """local_read over data written to a private (0o600) random work file, removed afterwards."""
    path=root/"work"/(uuid.uuid4().hex+suffix)
    path.touch(0o600)
    try:
        path.write_bytes(data)
        return local_read("invoice2data",path,root,extra=extra,timeout=timeout)
    finally:path.unlink(missing_ok=True)


def split_check_seconds():
    """The split check's time limit: INV_SPLIT_CHECK_TIMEOUT seconds, 20 when unset or not a number, 1 to 240."""
    try:value=int(os.getenv("INV_SPLIT_CHECK_TIMEOUT","") or 20)
    except ValueError:value=20
    return max(1,min(240,value))


def split_check(content,root,account=""):
    """multi_invoice.analyse_pdf over an uploaded PDF, in a reader child under the split check's own time limit.

    The API process never parses the upload or runs the page-text patterns itself; it gets back the plain outcome.
    Raises ChecksBusy when SPLIT_CHECK_CONCURRENCY checks are running, or all but one of them for this account.
    """
    with _split_checks_lock:
        if sum(_split_checks.values())>=SPLIT_CHECK_CONCURRENCY or \
                _split_checks.get(account,0)>=max(1,SPLIT_CHECK_CONCURRENCY-1):
            raise ChecksBusy()
        _split_checks[account]=_split_checks.get(account,0)+1
    try:return _work_file_read(content,".pdf",("--split-check",),root,split_check_seconds())["split"]
    finally:
        with _split_checks_lock:
            _split_checks[account]-=1
            if not _split_checks[account]:del _split_checks[account]


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


def process_job(path,options,store,ai_reader,progress=lambda *args:None,learned=None,filename=None,supplier_site=None):
    """process() in a job child, so the pattern work over a document's read text runs outside the API process.

    The child does every check between and after the reads: the native-text gates, evidence, the cross-read guards,
    the printed-column flags, the AI prompt's supplier examples and the evidence annotation. Everything with an effect
    stays here and the child asks for it one call at a time: the reader processes (local_read), the OCR and AI slots,
    the model-download memory, progress and the AI call, which holds the provider keys. Each stretch of the child's
    own work, from one answer to its next call, is held to the readers' time limit; past it the child is stopped and
    ChecksTimedOut ends the job, so a slow pattern stops one job, not the workers. The child also carries the readers'
    CPU limit. The child searches the live item master through the store here, limited to ``supplier_site`` (the
    reviewer's pick) when given (app/item_search.for_invoice). Returns process()'s result plus "annotated_evidence".
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
    catalog=LookupRulesSource(store)  # the live item master, read for the child's item searches
    def send(message):
        proc.stdin.write(json.dumps(message,default=str)+"\n");proc.stdin.flush()
    picks=[]  # (local read, what the AI picked), held until the child says the read stands: a rejected read adds no rule
    def _after_context(meta,read,local):
        """The search over what the AI read, and what it picked. A failure here costs them, not the read."""
        meta=dict(meta or {})
        try:
            found=meta.get("suggestions") or []
            meta["suggestions"]=found+ai_context.searched(catalog,read,supplier_site,found)
            picks.append((local or {},found))
        except Exception as e:progress("azure","AI suggestions unavailable",{"reason":type(e).__name__})
        return meta
    def keep_picks():
        kept=[]
        for known,found in picks:
            sites=[supplier_site] if supplier_site else item_search.invoice_sites(catalog,known)
            kept+=ai_rules.add(store,known,found,options.model,filename,sites)
        picks.clear()
        return kept
    def answer(call,args):
        if call=="capabilities":return capabilities()
        if call=="ai_rule_keep":return {"rules":keep_picks()}
        if call=="ai_rule_fill":return {"fills":ai_rules.fill(store,catalog,dict(args["invoice"]),supplier_site)}
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
            # Only the owner's Foundry is given the rules and the item master (ai_context); no other provider is.
            context=None
            if options.provider=="azure":
                local=dict(args.get("local") or {})
                try:context=ai_context.build(catalog,local,supplier_site,item_rules.rules())
                except Exception as e:progress("azure","AI context unavailable",{"reason":type(e).__name__})
            candidate,meta=ai_reader(path,str(args["text"]),options,examples=str(args["examples"]),**({"context":context} if context else {}))
            read=candidate.model_dump(mode="json")
            if context:meta=_after_context(meta,read,local)
            return {"invoice":read,"meta":meta}
        if call in ("item_search","item_sites"):
            # A failed search stops the item rules (item_rules.RulesError in the child), not the read.
            try:
                if call=="item_sites":return item_search.invoice_sites(catalog,dict(args["invoice"]))
                return item_search.search(catalog,dict(args["line"]),str(args["column"]),str(args["match"]))
            except item_rules.RulesError:raise
            except Exception as e:raise item_rules.RulesError(f"The item master search failed ({type(e).__name__})") from None
        raise ValueError("Unknown job call")
    try:
        send({"path":str(path),"root":str(store.root),"options":options.model_dump(mode="json"),
              "filename":filename,"learned":learned is not None,
              "supplier_site":supplier_site})
        while True:
            try:line=lines.get(timeout=timeout)
            except queue.Empty:
                raise ChecksTimedOut("The invoice checks timed out. Try another engine or inspect this document manually.") from None
            if line is None:raise ReaderFailed("The invoice checks stopped before finishing")
            if not line:raise ReaderFailed("The invoice checks sent a message over the size limit")
            message=json.loads(line)
            if "done" in message:
                try:ai_rules.count_filled(store,((message["done"].get("readers") or {}).get("ai") or {}).get("rules_applied"))
                except Exception as e:progress("azure","AI rule count unavailable",{"reason":type(e).__name__})
                return message["done"]
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
AI_NOT_ON_DOCUMENT="The AI gave a value here that is not on the document, so it was left blank"
AI_NOT_IN_OCR="The AI filled this, but the page OCR does not show it: check it against the source"
# The AI boundary (docs/AI_BOUNDARY.md). After the local readers and the item master rules (rules_first), whose
# resolved values are not gaps and are never overwritten, process() asks the AI only for:
#   fallback    the local result fails _basic_checks; the AI fills empty fields, and its lines stand in only for
#               local lines that do not sum to the net (on a weak scan read, its self-checked lines also when the
#               local lines do not align with them one to one);
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


def _ends_the_chain(invoice,missing):
    """A sound read that also prices every line. A line with no quantity or price is left out of the
    reconciliation, so its sum proves nothing, least of all on a zero-value invoice: the next reader still runs."""
    return (_basic_checks(invoice,missing) and invoice.net!=0
            and all(l.qty is not None and l.price is not None for l in invoice.lines))


def _rows_ahead(candidate,best):
    """How many more priced rows the candidate read than the best read, when both total zero and nothing else can
    tell: lines of no value sum to a net of 0 however many of them were read. A row with no quantity or price, or a
    code or item name, is no line, and once the best read has codes only a row with a code counts. Fewer item rows never
    win on a tie of coded rows: the rows it dropped are gone with no flag. On a tie, the read with fewer such junk rows
    is ahead. Otherwise 0, and the score decides."""
    if candidate is None or best is None or candidate.net!=0 or best.net!=0:return 0
    priced=lambda invoice:[l for l in invoice.lines if l.qty is not None and l.price is not None]
    items=lambda invoice:[l for l in priced(invoice) if l.sku or l.gtin or _names_an_item(l.description)]
    coded=lambda invoice:[l for l in items(invoice) if l.sku or l.gtin]
    if len(items(candidate))<len(items(best)):return -1
    rows=coded if coded(best) else items
    junk=lambda invoice:len(priced(invoice))-len(items(invoice))
    return len(rows(candidate))-len(rows(best)) or junk(best)-junk(candidate)


# A priced row that is a total or a tax is a summary row the reader took for a line.
_SUMMARY_ROW=re.compile(r"(?i)^\W*(?:(?:sub\s*|grand\s+|net\s+|page\s+|gross\s+)?total\b|(?:vat|tax|gst)\b)")
def _names_an_item(description):
    words=str(description or "")
    return bool(re.search(r"[^\W\d_]",words)) and not _SUMMARY_ROW.match(words)


def _same(field,mine,theirs):
    """Two readers agree when amounts are numerically equal or text matches by digits and letters."""
    if field in AMOUNT_FIELDS:
        try:return Decimal(str(mine))==Decimal(str(theirs))
        except (InvalidOperation,ValueError):return False
    if field=="uom":return uom_code(mine)==uom_code(theirs)
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


def _without(ai,absent):
    """The AI read less the values the document does not print."""
    out={**ai,"lines":[dict(line) for line in ai.get("lines") or []]}
    for n,field,_ in absent:
        (out if n is None else out["lines"][n])[field]=None
    return out


def _from_page_image(ai,evidence):
    """The AI read's evidence, every value named as read from the page image."""
    mark=lambda entry:{**(entry or {"quote":"","page":None}),"source":"ai","origin":scan_guard.AI_PAGE_IMAGE}
    rows=list(evidence.get("lines") or [])
    return {"header":{f:mark((evidence.get("header") or {}).get(f)) for f in READER_HEADER_FIELDS if ai.get(f) not in (None,"")},
            "lines":[{f:mark((rows[n] if n<len(rows) else {}).get(f)) for f in READER_LINE_FIELDS if line.get(f) not in (None,"")}
                     for n,line in enumerate(ai.get("lines") or [])]}


def _ai_filled(header_readers,line_readers):
    return ([f for f,reader in header_readers.items() if reader=="ai"]
            +[f"line {n+1} {f}" for n,row in enumerate(line_readers) for f,reader in row.items() if reader=="ai"])


def _ai_flagged(evidence):
    said=lambda entry:isinstance(entry,dict) and (entry.get("review") or {}).get("reason")==AI_DISAGREEMENT
    return ([f for f,entry in (evidence.get("header") or {}).items() if said(entry)]
            +[f"line {n+1} {f}" for n,row in enumerate(evidence.get("lines") or []) for f,entry in row.items() if said(entry)])


def _mark_absent(invoice,evidence,header_readers,line_readers,absent,scan,notes):
    """Say on the field what absent_values found. On native text the value was not written, so a field still blank
    carries it for the reviewer; on a scan an AI value stands, flagged, since the text it was checked against is OCR."""
    if not absent:return evidence
    evidence={"header":dict(evidence.get("header") or {}),"lines":[dict(x) for x in evidence.get("lines") or []]}
    lines=invoice.get("lines") or []
    while len(evidence["lines"])<len(lines):evidence["lines"].append({})
    said=[]
    for n,field,value in absent:
        if n is not None and n>=len(lines):continue
        holder=invoice if n is None else lines[n]
        rows=evidence["header"] if n is None else evidence["lines"][n]
        if scan:
            reader=header_readers.get(field) if n is None else (line_readers[n].get(field) if n<len(line_readers) else None)
            if reader!="ai":continue
            reason=AI_NOT_IN_OCR
        elif holder.get(field) in (None,""):reason=AI_NOT_ON_DOCUMENT
        else:continue
        rows[field]={**_review(rows.get(field),None if n is None else holder,"ai",reason,value)}
        said.append(field if n is None else f"line {n+1} {field}")
    if said and not scan:
        notes.append("The AI gave values the document does not print, so they were left blank: "+", ".join(said))
    return evidence


def merge_ai_fields(engine,ai,engine_evidence,ai_evidence,source,mode,reader=None,self_checked=False):
    """Fill the engines' empty fields from the AI read and flag every disagreement; never overwrite a value.

    ``engine`` and ``ai`` are invoice dicts; ``reader`` names the local engine in the reader maps (default ``source``). Lines merge pairwise when both reads have the same count and the
    pair aligns; in ``fallback``, when the engines' lines do not sum to their net, the AI's lines stand in if the
    counts differ and the AI's lines sum to the engines' net; otherwise the local lines are kept. ``self_checked``: a
    weak scan read, whose AI lines passed scan_guard.ai_self_check, so they stand in for open local lines whenever
    the two do not align one to one. Returns (invoice, evidence, header readers, line readers, notes).
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
    apart=len(ai_lines)!=len(engine_lines) or not all(map(_aligned,engine_lines,ai_lines))
    stand_in=(mode=="fallback" and (_engine_lines_open(engine) or unproved)
              and ((self_checked and apart) or (len(ai_lines)!=len(engine_lines) and _lines_reconcile(ai_lines,printed_net))))
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


def _printed_rate(invoice):
    """The one tax rate the printed totals imply, or None when they imply none."""
    net,tax=to_decimal(invoice.get("net")),to_decimal(invoice.get("tax"))
    if not net or tax is None:return None
    rate=tax/net
    return next((x for x in VAT_RATES if abs(rate-x)<=Decimal("0.0005")),None)


def _expected_amount(field,line,rate):
    """What the row's own quantity and unit price give for this amount, or None when they cannot."""
    qty,price=to_decimal(line.get("qty")),to_decimal(line.get("price"))
    if qty is None or price is None:return None
    if field=="net_amount":return qty*price
    percent=to_decimal(line.get("vat_pct"))
    share=percent/100 if percent is not None else rate
    if share is None:return None
    net=to_decimal(line.get("net_amount"))
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
        printed=to_decimal(invoice.get(printed_field))
        if printed is None:continue
        values=[to_decimal(line.get(field)) for line in lines]
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


def guard_figures(invoice,evidence,header_readers,source,text):
    """Net and tax against the page's own arithmetic: net + tax = the printed total, the lines = the net, or tax = a
    rate x the net. Wordings never end, so a figure proved by none is flagged, and one the printed total contradicts
    goes blank and flagged, as does an unproved whole number: nothing tells it from a count or a term. target_check holds
    the sheet's cells on the same arithmetic; this keeps a wrong figure out.
    """
    header=evidence.setdefault("header",{})
    net,tax=to_decimal(invoice.get("net")),to_decimal(invoice.get("tax"))
    totals=scan_guard.printed_totals(text)
    lines_prove=net is not None and scan_guard.lines_prove_net(invoice.get("lines"),net,text)
    whole=lambda value:scan_guard.printed_whole(value,text)
    # Below 100, a tie between two whole numbers is as likely to be two counts.
    rate_ties=net is not None and tax is not None and bool(_printed_rate(invoice)) and not (whole(net) and abs(net)<100)
    adds_up=disagrees=False
    if net is not None and tax is not None and totals:
        adds_up=any(abs(abs(net+tax)-total)<=AMOUNT_TOLERANCE for total in totals)
        disagrees=not adds_up
    # Over twice every printed total: no discount explains it, the figure lost its decimal mark. Under half of one:
    # no freight explains it, and lines read short sum to a page's subtotal as neatly as to the net.
    outsized=net is not None and bool(totals) and abs(net)>2*max(totals) and not lines_prove
    short=net is not None and bool(totals) and not adds_up and max(totals)>2*(abs(net)+abs(tax or 0))
    # A rate that ties leaves room for a charge on top, or for lines that prove the net; not for a total under them.
    contradicted=outsized or short or (disagrees and not (rate_ties and (lines_prove or abs(net+tax)<max(totals))))
    def settle(field,amount,proved,blank):
        if invoice.get(field) in (None,"") or (proved and not blank):return
        value=invoice.get(field)
        reason=("the printed total does not equal the net plus the tax, so it needs checking" if blank else
                "neither the printed total, the lines nor a tax rate prove this figure, so it needs checking")
        blank_it=blank or whole(amount)
        # A value that stays keeps the AI's flag: it names the other reading, which a person needs beside this one.
        if not blank_it and (header.get(field) or {}).get("review"):return
        header[field]=_review(header.get(field),None,source,reason,value,"contradicted" if blank else "unproven")
        if blank_it:invoice[field]=None;header_readers.pop(field,None)
    settle("net",net,adds_up or ((lines_prove or rate_ties) and not disagrees),short or (contradicted and not lines_prove))
    settle("tax",tax,adds_up or (rate_ties and not disagrees),contradicted)
    return invoice,evidence


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


def process(path,options,store,ai_reader,progress=lambda *args:None,item_master=None,ai_local=False,ai_rule_fill=None,ai_rule_keep=None):
    """``item_master(invoice) -> item_search``, and ``item_search(line) -> search(column, match)``, is the item master
    search matching.lookup_item runs per line (app/item_search.for_invoice). ``ai_local``: the AI reader also takes
    ``local=`` the local read so far, for the item master context the job's parent builds from it.
    ``ai_rule_fill(invoice) -> {"fills": [...]}`` is the rules the AI added (app.ai_rules), asked of the job's parent;
    ``ai_rule_keep() -> {"rules": [...]}`` adds the rules from this read's picks, once the read has passed its checks."""
    trace=[];best=None;best_score=-1;text="";boxes=[];selected="none";best_evidence={};best_source="native"
    # The engine id (or "ai") the reader maps name for the selected read; best_source is its evidence source.
    best_reader="none"
    document_type_hint=None;extraction_note=None;native_review=[]
    ai_attempted=False;invoice2data_ocr=False;ai_calls=0;ai_status="off";ai_notes=[];readers_maps=None;ai_suggestions=[];ai_rules_added=[];ai_rules_applied=[]
    # A scan (no usable text layer) is told by the PDF's own text; its OCR boxes corroborate AI barcodes.
    native_chars=None;ocr_boxes=[];scan=False;ai_reason=None
    # The text and boxes of the selected local read: the target check re-checks its values against them.
    selected_page=None;selected_tables=[];page_tables=[]
    # Every local read of this document, so a required field the selected read missed can still be
    # taken from the reader that printed it.
    local_reads=[];selected_read=None;filled_fields={}
    # The item master rules' result on the selected local read (rules_first.Resolved), once they ran.
    resolved=None;lines_stood_in=False
    # Readers the chain reached but that are not installed: told on the job with their install hint, and
    # named on the AI's trace entry as why it ran.
    missing_readers=[]
    cross_check=bool(getattr(options,"ai_cross_check",False)) or os.getenv("INV_STUDIO_AI_CROSS_CHECK","0")=="1"
    def call_ai(role):
        """One AI read: (candidate, its own header evidence, completeness) with a trace entry, or None."""
        nonlocal ai_attempted,ai_calls,ai_suggestions
        ai_attempted=True;ai_calls+=1
        progress(options.provider,"Waiting for an AI reader slot")
        _AI_SLOTS.acquire()
        progress(options.provider,"AI is reading the invoice")
        start=time.monotonic()
        try:
            sent={"local":best.model_dump(mode="json") if best is not None else {}} if ai_local else {}
            candidate,meta=ai_reader(path,text,options,**sent)
            meta=dict(meta or {});ai_evidence=meta.pop("evidence",None) or {}
            ai_suggestions=meta.pop("suggestions",None) or []
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

        On a weak local read of a scan (``fallback``) the AI's read must first pass its own checks. A value the
        document's text does not print is not written; on a scan, whose text is OCR, it stands flagged."""
        nonlocal best,best_score,selected,best_evidence,best_source,best_reader,ai_status,ai_notes,readers_maps,ai_reason,lines_stood_in
        read=call_ai(mode)
        if read is None:
            ai_status="failed";return
        candidate,ai_evidence,score=read
        ai_dict=candidate.model_dump(mode="json")
        ai_status=mode
        local=best.model_dump(mode="json") if best is not None else None
        # Only a local read that failed the basic checks is weak: one that passed goes through gap_fill.
        weak=scan_guard.weak_read(local) if scan and mode=="fallback" else []
        if weak:
            # When the AI's read of the page image fails its own checks too, nothing is filled and the invoice is not read.
            failed=scan_guard.ai_self_check(ai_dict)
            if failed:
                ai_status="not_read"
                ai_reason=("Not read: the local OCR read is weak ("+"; ".join(weak)+") and the AI read fails its checks ("
                           +"; ".join(failed)+"). Nothing was filled; enter the invoice manually.")
                return
        if weak:
            # A money total stands only with the quote it was read from: no derived net or tax, no 0 for unprinted.
            ai_dict={**ai_dict,**{f:None for f in ("net","tax") if not str((ai_evidence.get(f) or {}).get("quote") or "").strip()}}
        absent=ai_context.absent_values(ai_dict,ai_evidence,text)
        if not scan:ai_dict=_without(ai_dict,absent)
        ai_ev=build_evidence(ai_dict,boxes,"ai",header=ai_evidence)
        if best is None and not weak:
            best=Invoice.model_validate(ai_dict);best_score=quality(best)[0];selected=f"{options.provider} / {options.model}";best_source=best_reader="ai";best_evidence=ai_ev
            ai_reason=AI_REASONS["whole"]
            header_readers,line_readers=_reader_map(ai_dict,"ai")
        else:
            if local is None:best_source=best_reader="ai"
            if weak:ai_ev=_from_page_image(ai_dict,ai_ev)
            merged,evidence,header_readers,line_readers,notes=merge_ai_fields(local or Invoice().model_dump(mode="json"),ai_dict,best_evidence if local else {},ai_ev,best_source,mode,best_reader,self_checked=bool(weak))
            lines_stood_in=bool(ai_dict.get("lines")) and merged["lines"]==ai_dict["lines"] and merged["lines"]!=(local or {}).get("lines")
            best=Invoice.model_validate(merged);best_score=quality(best)[0];best_evidence=evidence
            readers_maps=(header_readers,line_readers);ai_notes=notes
        best_evidence=_mark_absent(best.model_dump(mode="json"),best_evidence,header_readers,line_readers,absent,scan,ai_notes)
        if weak:
            if ai_dict.get("lines") and line_readers and all(set(row.values())=={"ai"} for row in line_readers):
                selected=f"{options.provider} / {options.model}"
            filled=_ai_filled(header_readers,line_readers);flagged=_ai_flagged(best_evidence)
            what=(f"read the whole invoice ({len(ai_dict.get('lines') or [])} lines and {len(header_readers)} header fields)" if local is None else
                  "filled only the empty fields ("+(", ".join(filled) or "none")+") and flagged what it reads differently ("+(", ".join(flagged) or "nothing")+")")
            ai_reason=(f"The local OCR read of this scan is weak ({'; '.join(weak)}); the AI read the page image, "
                       f"passed its own checks and {what}")
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
            # On a tied score a read that passes the basic checks beats one that does not: the gaps count the
            # same, but a missing number or an unreconciled table is not the same as a missing date.
            rows_ahead=_rows_ahead(candidate,best)
            if candidate is not None and rows_ahead>=0 and (rows_ahead>0 or score>best_score or (
                    score==best_score and (engine==native_preflight_engine or (
                        _basic_checks(candidate,_open_gaps(candidate,missing))
                        and not _basic_checks(best,_open_gaps(best,quality(best)[1])))))):
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
            if options.engine in ("auto","ai") and engine=="paddleocr" and _ends_the_chain(candidate,_open_gaps(candidate,missing)):
                for remaining in chain[chain.index(engine)+1:]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"Invoice fields were read. Remaining exceptions go to the selected AI or manual review."})
                break
            if not missing and candidate.net!=0:break
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
        # A line the item master rules identified has its item: an unprinted code is not a gap for the AI.
        gaps=[x for x in gaps if not (re.fullmatch(r"line (\d+) item identity",x)
                                      and rules_first.identified(resolved,int(x.split()[1])))]
        return _open_gaps(invoice,gaps)
    def added_rule_fills(invoice):
        """What the AI-added rules fill on this read, less anything the workbook's rules decide themselves: a Found or
        Conflict result stands, and an Ambiguous or Suggestion one only lets through an item it already lists."""
        if ai_rule_fill is None or not (options.ai_fallback or options.engine=="ai"):return []
        try:
            ruleset=item_rules.rules()
            fills=ai_rule_fill(invoice)["fills"]
            search=item_master(invoice) if item_master else None
            kept=[]
            for fill in fills:
                if str(invoice["lines"][fill["line"]].get("item_id") or "").strip():continue
                decided=matching.lookup_item(search(invoice["lines"][fill["line"]]),ruleset) if search else {"status":"","parents":[]}
                if decided["status"] in ("Found","Conflict"):continue
                if decided["status"] in ("Ambiguous","Suggestion") and fill["item_id"] not in map(str,decided["parents"]):continue
                kept.append(fill)
            return kept
        except Exception as e:
            trace.append({"engine":"ai_rules","status":"unavailable","reason":"The AI-added rules did not run: "+type(e).__name__})
            return []
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
    # The item master rules run on the local read before the AI is asked: what they resolve is not a gap.
    if best is not None:
        try:
            ruleset=item_rules.rules();read=best.model_dump(mode="json")
            resolved=rules_first.resolve(read,item_master(read) if item_master else None,ruleset)
        except item_rules.RulesError as e:
            trace.append({"engine":"item_rules","status":"unavailable","reason":"The item master rules did not run: "+str(e)[:200]})
        else:
            filled=[*resolved.sources["header"],*(f"line {n} {f}" for n,line in enumerate(resolved.sources["lines"],1) for f in line)]
            trace.append({"engine":"item_rules","status":"resolved" if filled else "nothing_resolved",
                          "version":getattr(ruleset,"version",""),"fields":filled,"held":resolved.held,
                          **({} if item_master else {"reason":"No item master matcher is connected; item lookups did not run"})})
            # What an AI-added rule covers is not a gap: the AI need not be asked, and protect keeps it through a merge.
            for fill in added_rule_fills(resolved.invoice):
                resolved.invoice["lines"][fill["line"]]["item_id"]=fill["item_id"]
                resolved.sources["lines"][fill["line"]]["item_id"]="ai"
                ai_rules_applied.append(fill)
            best=Invoice.model_validate(resolved.invoice);best_score=quality(best)[0]
    missing=gaps_of(best);ai_reason=None
    # A line the rules could not identify is what the AI's wider search is for; a Conflict is the reviewer's to decide.
    # Only Azure AI Foundry is given that search, so no other provider is called for it.
    open_items=[h["line"] for h in (resolved.held if resolved and options.provider=="azure" else [])
                if h["status"]!="Conflict" and not str(resolved.invoice["lines"][h["line"]-1].get("item_id") or "").strip()]
    # What the engines' result asks of the AI, recorded whether or not the AI runs, so AI calls per invoice
    # can be counted on an AI-off run: fallback when the engines could not read, gap fill when they left gaps,
    # cross-check (an extra call on a complete invoice) only when the owner switched it on.
    need=None if document_type_hint=="possible_purchase_order" else (
        "fallback" if not _basic_checks(best,missing) else "gap_fill" if missing or open_items else "cross_check")
    if document_type_hint=="possible_purchase_order":
        trace.append({"engine":options.provider,"status":"skipped","reason":"The document is labelled Purchase Order; invoice extraction needs the supplier invoice."})
        ai_status,ai_reason="skipped",trace[-1]["reason"]
    elif options.engine=="ai":
        if not options.model:
            trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in Engines & AI"});ai_status="unavailable"
        elif not ai_attempted:read_ai()
    elif options.ai_fallback:
        if not options.model:
            if missing or open_items:trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in Engines & AI"})
            ai_status="unavailable" if missing or open_items else "skipped"
            if not missing and not open_items:ai_reason="The local readers read a complete, reconciled invoice; no model is selected"
        elif need=="cross_check" and not cross_check:ai_status="skipped"
        else:merge_ai(need)
    if resolved is not None and best is not None and lines_stood_in:
        # The lines are other rows now, so nothing found for the local ones goes back on them by position.
        merged=best.model_dump(mode="json");ai_rules_applied=[]
        try:rules_first.rerun_lines(resolved,merged["lines"],item_master(merged) if item_master else None,item_rules.rules())
        except item_rules.RulesError as e:
            trace.append({"engine":"item_rules","status":"unavailable","reason":"The item master rules did not run on the AI's lines: "+str(e)[:200]})
            rules_first.rerun_lines(resolved,merged["lines"],None,None)
    if resolved is not None and best is not None and ai_calls:
        best=Invoice.model_validate(rules_first.protect(best.model_dump(mode="json"),resolved))
    if ai_status=="failed":ai_reason="The AI read failed: "+str(trace[-1].get("reason") or "no reason given")[:240]
    if ai_status=="not_read":extraction_note=ai_reason
    if best is not None:
        final=best.model_dump(mode="json")
        header_map,line_map=readers_maps or _reader_map(final,best_reader)
        if resolved is not None:
            header_map.update(resolved.sources["header"])
            if len(line_map)==len(resolved.sources["lines"]):
                for readers_of,filled in zip(line_map,resolved.sources["lines"]):readers_of.update(filled)
        for field,read in filled_fields.items():
            if final.get(field) not in (None,""):header_map[field]=read["engine"]
        final,best_evidence=guard_lines(final,dict(best_evidence or {}),line_map,best_source,scan,ocr_boxes)
        final,best_evidence=guard_amounts(final,best_evidence,line_map,best_source)
        final,best_evidence=guard_figures(final,best_evidence,header_map,best_source,text)
        final,best_evidence=guard_header(final,best_evidence,header_map,best_source,text)
        # Lines the AI read, and rules this read's AI just added, run here: the same rules, on what the merge left empty.
        if ai_calls:
            if ai_rule_keep is not None and ai_status in ("fallback","gap_fill","cross_check","selected"):
                try:ai_rules_added=ai_rule_keep()["rules"]
                except Exception as e:trace.append({"engine":"ai_rules","status":"unavailable","reason":"The AI's picks were not kept as rules: "+type(e).__name__})
            for fill in added_rule_fills(final):
                final["lines"][fill["line"]]["item_id"]=fill["item_id"]
                if fill["line"]<len(line_map):line_map[fill["line"]]["item_id"]="ai"
                ai_rules_applied.append(fill)
        best=Invoice.model_validate(final);best_score=quality(best)[0];readers_maps=(header_map,line_map)
    # fields: the gaps that asked for the AI. It may fill any other empty field too; "filled" lists what it did fill.
    ai_record={"status":ai_status,"reason":ai_reason or AI_REASONS.get(ai_status,""),"calls":ai_calls,"need":need,
               "fields":[*missing,*(f"line {n} item_id" for n in open_items)]}
    if missing_readers and need in ("fallback","gap_fill"):
        # A reader this file needed is missing: that, not the document, is why the AI is asked.
        hint=_install_hint(missing_readers[0])
        ai_record["reason"]=", ".join(missing_readers)+": "+hint[0].lower()+hint[1:]+". "+ai_record["reason"]
    if ai_notes:ai_record["notes"]=ai_notes
    invoice=(best or Invoice()).model_dump(mode="json")
    header_readers,line_readers=readers_maps or _reader_map(invoice,best_reader)
    # What a reviewer and the receipt must see: the cells the AI wrote, what it reads differently, what it only suggests.
    if ai_status in ("fallback","gap_fill","cross_check"):
        ai_record.update({k:v for k,v in (("filled",_ai_filled(header_readers,line_readers)),("flagged",_ai_flagged(best_evidence or {}))) if v})
    elif ai_rules_applied:ai_record["filled"]=_ai_filled(header_readers,line_readers)
    ai_record.update({k:v for k,v in (("suggestions",ai_suggestions),("rules_added",ai_rules_added),("rules_applied",ai_rules_applied)) if v})
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
