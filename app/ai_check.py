"""The AI's own read of an invoice, compared with the local read (owner order, 6 Nov; docs/AI_BOUNDARY.md).

Every invoice gets one AI read when Foundry is on. The AI never sees the local values, so its agreement is a second
read, not a yes. Afterwards each cell is one of: confirmed (both reads match), filled (the local read was blank, the
value is the AI's), disputed (they differ: blocks the export until a person picks) or unchecked (one read has no value,
or the text differs where wording may vary). Both reads agreeing does not prove a value right, so nothing here says
verified. The record is written once per read; what a cell shows comes from view(), against the value it holds now, so
a cell someone changed stops showing the mark.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .models import uom_code

HEADER_FIELDS=("number","supplier_name","buyer_name","po","date_printed","date","currency","net","tax")
LINE_FIELDS=("sku","gtin","description","qty","uom","price","net_amount","tax_amount")
AMOUNT_FIELDS={"net","tax","qty","price","net_amount","tax_amount"}
# Printed text that can differ between two good reads: a difference is shown, not a dispute. date_printed only when
# the date read from it matches.
SOFT_FIELDS={"description"}
CONFIRMED,FILLED,DISPUTED,SETTLED,UNCHECKED="confirmed","filled","disputed","settled","unchecked"
CHOICES=("local","ai","typed")
ISSUE="AI Dispute"
LINES_KEY="lines"
STATUS_LABELS={"confirmed":"AI confirmed","partial":"Partly AI confirmed","disputed":"In dispute","not_confirmed":"Not AI-confirmed"}
# Why no read was compared, by readers.ai.status. Fixed words: a provider's own error text stays on the review screen.
NOT_READ={"off":"AI is off","unavailable":"no AI model is selected","skipped":"no AI read was made",
          "failed":"the AI read failed or timed out","not_read":"the AI read failed its own checks",
          "selected":"the AI was the only reader, so there is nothing to compare"}


def _blank(value):
    return value in (None,"")


def _folded(value):
    """Letters and digits of any script, with Arabic-Indic digits as digits: two Arabic names are not both empty."""
    text=unicodedata.normalize("NFKC",str(value)).casefold()
    return re.sub(r"[\W_]","","".join(str(unicodedata.decimal(c,c)) for c in text))


def same(field,mine,theirs):
    """Two reads agree when amounts are numerically equal or text matches by letters and digits."""
    if field in AMOUNT_FIELDS:
        try:return Decimal(str(mine))==Decimal(str(theirs))
        except (InvalidOperation,ValueError):return False
    if field=="uom":return uom_code(mine)==uom_code(theirs)
    return _folded(mine)==_folded(theirs)


def _text(value):
    return str(value)[:300]


def _cell(field,mine,theirs,now,reader,soft=False):
    """The record of one cell, or None when neither read holds a value. ``now`` is what the merge left in the cell."""
    if _blank(mine):
        if reader=="ai" and not _blank(now):return {"state":FILLED,"value":_text(now)}
        return None
    if _blank(theirs):return {"state":UNCHECKED,"value":_text(mine)}
    if same(field,mine,theirs):return {"state":CONFIRMED,"value":_text(mine)}
    if soft or field in SOFT_FIELDS:return {"state":UNCHECKED,"value":_text(mine),"ai":_text(theirs)}
    return {"state":DISPUTED,"local":_text(mine),"ai":_text(theirs)}


def _rows(lines):
    return [{f:line[f] for f in LINE_FIELDS if not _blank(line.get(f))} for line in lines]


def not_read(status):
    """The record of an invoice with no AI read to compare."""
    return {"read":False,"why":NOT_READ.get(status,NOT_READ["skipped"])}


def compare(local,ai,final,header_readers,line_readers):
    """Compare the two reads of one invoice by field and by line. Lines pair by position; different counts are one
    dispute over the lines as a whole, holding both, since no row of one read answers to a row of the other."""
    local,ai=local or {},ai or {}
    soft_date=not _blank(local.get("date")) and not _blank(ai.get("date")) and same("date",local["date"],ai["date"])
    header={}
    for field in HEADER_FIELDS:
        cell=_cell(field,local.get(field),ai.get(field),final.get(field),header_readers.get(field),soft=field=="date_printed" and soft_date)
        if cell:header[field]=cell
    mine,theirs,now=[list(x.get("lines") or []) for x in (local,ai,final)]
    counts_differ=bool(mine) and bool(theirs) and len(mine)!=len(theirs)
    lines=[]
    for n,row in enumerate(now):
        readers=line_readers[n] if n<len(line_readers) else {}
        one=mine[n] if n<len(mine) else {}
        other=theirs[n] if n<len(theirs) else {}
        if counts_differ:
            # Which read stands in the rows now is what the readers say; neither can check the other by position.
            one,other=({},row) if all(reader=="ai" for reader in readers.values()) and readers else (row,{})
        cells={f:cell for f in LINE_FIELDS if (cell:=_cell(f,one.get(f),other.get(f),row.get(f),readers.get(f)))}
        if readers.get("item_id")=="ai" and not _blank(row.get("item_id")):cells["item_id"]={"state":FILLED,"value":_text(row["item_id"])}
        lines.append(cells)
    record={"read":True,"header":header,"lines":lines,"settled":{}}
    if counts_differ:record["line_reads"]={"local":_rows(mine),"ai":_rows(theirs)}
    return record


def _label(key):
    return key[:1].upper()+key[1:].replace("_"," ")


def disputes(record):
    """Every dispute the read found, settled or not: [{key, label, line, field, local, ai}]; the key names it to settle."""
    if not (record or {}).get("read"):return []
    found=[{"key":f,"line":None,"field":f,"local":c["local"],"ai":c["ai"]} for f,c in record["header"].items() if c["state"]==DISPUTED]
    for n,row in enumerate(record["lines"]):
        found+=[{"key":f"line {n+1} {f}","line":n+1,"field":f,"local":c["local"],"ai":c["ai"]} for f,c in row.items() if c["state"]==DISPUTED]
    both=record.get("line_reads")
    if both:found.append({"key":LINES_KEY,"line":None,"field":None,"local":f"{len(both['local'])} lines","ai":f"{len(both['ai'])} lines",
                          "local_lines":both["local"],"ai_lines":both["ai"]})
    return [{**x,"label":_label(x["key"])} for x in found]


def _state(field,cell,current,settled,key):
    if key in settled:return SETTLED
    if cell["state"]==DISPUTED:return DISPUTED
    return cell["state"] if not _blank(current) and same(field,cell["value"],current) else None


def view(readers,invoice):
    """What this invoice's cells show now: the status, each cell's state and the disputes still open."""
    record=(readers or {}).get("ai_check")
    if not (record or {}).get("read"):
        return {"status":"not_confirmed","label":STATUS_LABELS["not_confirmed"],"why":(record or {}).get("why") or NOT_READ["skipped"],
                "header":{},"lines":[],"disputes":[],"settled":{},"counts":{}}
    settled=record.get("settled") or {}
    header={f:s for f,c in record["header"].items() if (s:=_state(f,c,invoice.get(f),settled,f))}
    rows=invoice.get("lines") or []
    lines=[]
    # A row added or removed moves every row after it, so marks stand only while the count holds. A dispute stays open.
    same_count=len(rows)==len(record["lines"])
    for n,row in enumerate(record["lines"]):
        states={}
        for f,c in row.items():
            key=f"line {n+1} {f}"
            if key in settled or c["state"]==DISPUTED:states[f]=_state(f,c,None,settled,key)
            elif same_count and (s:=_state(f,c,rows[n].get(f),settled,key)):
                states[f]=SETTLED if s==FILLED and LINES_KEY in settled else s
        lines.append(states)
    open_ones=[x for x in disputes(record) if x["key"] not in settled]
    found=[*header.values(),*(s for row in lines for s in row.values())]
    counts={s:found.count(s) for s in (CONFIRMED,FILLED,SETTLED,UNCHECKED)}
    counts[DISPUTED]=len(open_ones)
    held=[*record["header"].values(),*(c for row in record["lines"] for c in row.values())]
    # A cell that lost its mark, a cell only one read holds: either way the whole invoice was not confirmed.
    whole=len(found)==len(held) and all(s==CONFIRMED for s in found) and not record.get("line_reads")
    status="disputed" if open_ones else "not_confirmed" if not counts[CONFIRMED] and not counts[SETTLED] else "confirmed" if whole else "partial"
    return {"status":status,"label":STATUS_LABELS[status],"why":"","header":header,"lines":lines,"disputes":open_ones,"settled":settled,"counts":counts}


def issues(check):
    """One blocking issue per open dispute. Hard: a review confirm does not accept it, only a pick settles it. The values
    stay on the review screen and out of the message, which reaches logs and receipts."""
    return [{"code":ISSUE,"message":f"{x['label']}: the local read and the AI read differ. Pick a value in the AI check box.",
             "owner":"Reviewer","line":x["line"],"field":x["field"],"blocking":True,"level":"hard"} for x in check["disputes"]]


def _current(key,invoice):
    if key==LINES_KEY:return None
    if not key.startswith("line "):return invoice.get(key)
    _,n,field=key.split(" ",2)
    rows=invoice.get("lines") or []
    return rows[int(n)-1].get(field) if int(n)<=len(rows) else None


def settle(readers,picks,invoice,who,at):
    """The readers with a person's picks recorded: {key: "local"|"ai"|"typed"}. A pick must name an open dispute and the
    invoice must hold what was picked (a row since removed holds nothing to check). Raises ValueError otherwise."""
    if not picks:return readers
    record=(readers or {}).get("ai_check") or {}
    known={x["key"]:x for x in disputes(record)}
    settled=dict(record.get("settled") or {})
    rows=invoice.get("lines") or []
    for key,choice in picks.items():
        dispute=known.get(key)
        if dispute is None or choice not in CHOICES:raise ValueError("That is not an open AI dispute. Refresh and try again.")
        if key==LINES_KEY:
            if choice!="typed" and len(rows)!=len(dispute[f"{choice}_lines"]):raise ValueError("The lines do not match the read you picked.")
            value=f"{len(rows)} lines"
        else:
            value=_current(key,invoice)
            present=dispute["line"] is None or dispute["line"]<=len(rows)
            if choice!="typed" and present and (_blank(value) or not same(dispute["field"],dispute[choice],value)):
                raise ValueError(f"{dispute['label']} does not hold the value you picked.")
        settled[key]={"choice":choice,"value":None if _blank(value) else _text(value),"by":who,"at":at}
    return {**readers,"ai_check":{**record,"settled":settled}}


def learnable(invoice,readers):
    """The invoice as learning may see it: a value only the AI read, that no person settled, is left out, so one wrong
    AI read cannot teach a wrong rule. Disputes cannot reach here unsettled: they block the export."""
    check=view(readers,invoice)
    out={**invoice,**{f:None for f,s in check["header"].items() if s==FILLED},"lines":[dict(x) for x in invoice.get("lines") or []]}
    for row,states in zip(out["lines"],check["lines"]):
        for f,s in states.items():
            if s==FILLED:row[f]=None
    return out
