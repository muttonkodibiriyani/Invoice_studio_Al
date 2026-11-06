"""The AI's own read of an invoice, compared with the local read (owner order, 6 Nov; docs/AI_BOUNDARY.md).

Every invoice gets one AI read when Foundry is on. The AI never sees the local values, so its agreement is a second
read, not a yes. Afterwards each cell is one of: confirmed (both reads match), filled (the local read was blank, the
value is the AI's), disputed (they differ: blocks the export until a person picks) or unchecked (one read has no value,
or two wordings of the same goods). Both reads agreeing does not prove a value right, so nothing here says
verified. The record is written once per read; what a cell shows comes from view(), against the value it holds now, so
a cell someone changed stops showing the mark.
"""
import unicodedata
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from .models import Line, uom_code

HEADER_FIELDS=("number","supplier_name","buyer_name","po","date_printed","date","currency","net","tax")
LINE_FIELDS=("sku","gtin","description","qty","uom","price","net_amount","tax_amount")
ROW_IDENTITY=("item_id","sku","gtin","description")  # what makes a row the goods it was compared as
AMOUNT_FIELDS={"net","tax","qty","price","net_amount","tax_amount"}
# Printed text that can differ between two good reads: a difference is shown, not a dispute. A description only while
# the two name the same goods, date_printed only when the date read from it matches.
SOFT_FIELDS={"description"}
# What says nothing about which goods a description names; a word with fewer letters than this says nothing either.
COMMON_WORDS={"and","for","the","with"}
MIN_WORD_LETTERS=3
WORD_CLOSE=0.8
OCR_LOOKALIKES=str.maketrans("015","ols")
# The read fields each target cell shows, its own first. The rest only pass on a dispute: the AI did not read what the
# rules derived from them. A cell with no own field (None) never shows an agreement.
TARGET_HEADER={"number":("number",),"site":(None,"supplier_name"),"po":("po",),"location":(None,"buyer_name"),"date":("date",),"net":("net",),"tax":("tax",)}
TARGET_LINES={"Item":("item_id","sku","gtin"),"UPC":("gtin",),"Unit Cost":("price",),"Quantity":("qty",)}
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
    """Letters, digits and marks of any script, Arabic-Indic digits as digits, each run of separators as one mark: SI/123
    is SI-123, but 12-3 is not 1-23 and 5.5 is not 55. Marks stay, since a Hindi vowel sign makes another word; Arabic
    ones go, since they are optional in print."""
    kept=[]
    for c in unicodedata.normalize("NFKC",str(value)).casefold():
        kind=unicodedata.category(c)
        if kind[0] in "LN":kept.append(str(unicodedata.decimal(c,c)))
        elif kind[0]=="M":kept.append("" if unicodedata.name(c,"").startswith("ARABIC") else c)
        elif kind!="Cf":kept.append(" ")  # a format character (a direction mark, a soft hyphen) separates nothing
    return "-".join("".join(kept).split())


def same(field,mine,theirs):
    """Two reads agree when amounts are numerically equal or text matches by the above. Text with nothing left to
    compare agrees only with itself: $ and $ agree, --- and /// do not."""
    if field in AMOUNT_FIELDS:
        try:return Decimal(str(mine))==Decimal(str(theirs))
        except (InvalidOperation,ValueError):return False
    if field=="uom":return uom_code(mine)==uom_code(theirs)
    if str(mine).strip() and str(mine).strip()==str(theirs).strip():return True
    folded=_folded(mine)
    return bool(folded) and folded==_folded(theirs)


def _words(value):
    return [w.translate(OCR_LOOKALIKES) for w in _folded(value).split("-") if w not in COMMON_WORDS and sum(c.isalpha() for c in w)>=MIN_WORD_LETTERS]


def _same_goods(mine,theirs):
    """Whether two descriptions could name the same goods: they share a significant word, allowing for OCR's misread
    letters and lost spaces. One with no significant word cannot say otherwise."""
    one,other=_words(mine),_words(theirs)
    if not one or not other:return True
    joined="".join(other),"".join(one)
    return any(w in joined[0] for w in one) or any(w in joined[1] for w in other) or any(SequenceMatcher(None,a,b).ratio()>=WORD_CLOSE for a in one for b in other)


def _text(value):
    return str(value)[:300]


def _cell(field,mine,theirs,now,reader,soft=False):
    """The record of one cell, or None when neither read holds a value. ``now`` is what the merge left in the cell."""
    if _blank(mine):
        if reader=="ai" and not _blank(now):return {"state":FILLED,"value":_text(now)}
        return None
    if _blank(theirs):return {"state":UNCHECKED,"value":_text(mine)}
    if same(field,mine,theirs):return {"state":CONFIRMED,"value":_text(mine)}
    if soft or field in SOFT_FIELDS and _same_goods(mine,theirs):return {"state":UNCHECKED,"value":_text(mine),"ai":_text(theirs)}
    return {"state":DISPUTED,"local":_text(mine),"ai":_text(theirs)}


def _rows(lines):
    """A read's whole rows, since picking them replaces the invoice's: the item, part code, evidence and page go too."""
    return [{f:line[f] for f in Line.model_fields if not _blank(line.get(f))} for line in lines]


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
    identities=[{f:_text(row[f]) for f in ROW_IDENTITY if not _blank(row.get(f))} for row in now]
    record={"read":True,"header":header,"lines":lines,"settled":{},"identities":identities}
    if counts_differ:record["line_reads"]={"local":_rows(mine),"ai":_rows(theirs)}
    return record


def _label(key):
    return key[:1].upper()+key[1:].replace("_"," ")


def disputes(record):
    """Every dispute the read found or an earlier read left open, settled or not: [{key, label, line, field, local, ai}];
    the key names it to settle."""
    record=record or {}
    found=[{"key":f,"line":None,"field":f,"local":c["local"],"ai":c["ai"]} for f,c in record.get("header",{}).items() if c["state"]==DISPUTED]
    for n,row in enumerate(record.get("lines",[])):
        found+=[{"key":f"line {n+1} {f}","line":n+1,"field":f,"local":c["local"],"ai":c["ai"]} for f,c in row.items() if c["state"]==DISPUTED]
    both=record.get("line_reads")
    if both:found.append({"key":LINES_KEY,"line":None,"field":None,"local":f"{len(both['local'])} lines","ai":f"{len(both['ai'])} lines",
                          "local_lines":both["local"],"ai_lines":both["ai"]})
    return [{**x,"label":_label(x["key"])} for x in found]+record.get("carried",[])


def _answered(record,dispute):
    """Whether a new read compared this cell: it agreed, or it holds its own dispute there. A read that was not made, or
    has no value there, says nothing about it."""
    if not record.get("read"):return False
    if dispute["key"]==LINES_KEY:
        return "line_reads" in record or any(c["state"]==CONFIRMED for row in record["lines"] for c in row.values())
    n=dispute["line"]
    if n and n>len(record["lines"]):return False
    cell=(record["lines"][n-1] if n else record["header"]).get(dispute["field"])
    return bool(cell) and cell["state"] in (CONFIRMED,DISPUTED)


def carry(old,new):
    """The record of a re-read: a dispute nobody settled stays open unless the new AI read compared that cell, so a retry
    with the AI off or failing cannot clear it."""
    new=new or not_read("skipped")
    settled=(old or {}).get("settled") or {}
    kept=[x for x in disputes(old) if x["key"] not in settled and not _answered(new,x)]
    return {**new,"carried":kept} if kept else new


def _state(field,cell,current,settled,key):
    # Settled shows only while the cell holds what the person settled: a later edit is not what they looked at.
    if key in settled:
        value=settled[key].get("value")
        return SETTLED if not _blank(value) and not _blank(current) and same(field,value,current) else None
    if cell["state"]==DISPUTED:return DISPUTED
    return cell["state"] if not _blank(current) and same(field,cell["value"],current) else None


def _same_goods_row(record,n,row):
    """Whether row n still holds the goods the reads compared, a pick on its identity counted. A record from before
    identities were kept cannot say otherwise."""
    identities=record.get("identities")
    if identities is None or n>=len(identities):return identities is None
    settled=record.get("settled") or {}
    for f in ROW_IDENTITY:
        key=f"line {n+1} {f}"
        compared=settled[key].get("value") if key in settled else identities[n].get(f)
        if _blank(compared)!=_blank(row.get(f)) or not _blank(compared) and not same(f,compared,row[f]):return False
    return True


def view(readers,invoice):
    """What this invoice's cells show now: the status, each cell's state and the disputes still open."""
    record=(readers or {}).get("ai_check")
    if not (record or {}).get("read") and not (record or {}).get("carried"):
        return {"status":"not_confirmed","label":STATUS_LABELS["not_confirmed"],"why":(record or {}).get("why") or NOT_READ["skipped"],
                "header":{},"lines":[],"disputes":[],"settled":{},"counts":{}}
    settled=record.get("settled") or {}
    header={f:s for f,c in record.get("header",{}).items() if (s:=_state(f,c,invoice.get(f),settled,f))}
    rows=invoice.get("lines") or []
    lines=[]
    # A row added or removed moves every row after it, so marks stand only while the count holds, and each row's only
    # while it holds the same goods: a quantity agreed for one item says nothing of another. A dispute stays open.
    same_count=len(rows)==len(record.get("lines",[]))
    for n,row in enumerate(record.get("lines",[])):
        states={}
        for f,c in row.items():
            if s:=_state(f,c,rows[n].get(f) if same_count and _same_goods_row(record,n,rows[n]) else None,settled,f"line {n+1} {f}"):
                states[f]=SETTLED if s==FILLED and LINES_KEY in settled else s
        lines.append(states)
    open_ones=[x for x in disputes(record) if x["key"] not in settled]
    for x in record.get("carried",[]):
        if x["key"] in settled or not x["field"]:continue
        if not x["line"]:header[x["field"]]=DISPUTED
        elif x["line"]<=len(lines):lines[x["line"]-1][x["field"]]=DISPUTED
    found=[*header.values(),*(s for row in lines for s in row.values())]
    counts={s:found.count(s) for s in (CONFIRMED,FILLED,SETTLED,UNCHECKED)}
    counts[DISPUTED]=len(open_ones)
    held=[*record.get("header",{}).values(),*(c for row in record.get("lines",[]) for c in row.values())]
    # A cell that lost its mark, a cell only one read holds: either way the whole invoice was not confirmed.
    whole=len(found)==len(held) and all(s==CONFIRMED for s in found) and not record.get("line_reads")
    status="disputed" if open_ones else "not_confirmed" if not counts[CONFIRMED] and not counts[SETTLED] else "confirmed" if whole else "partial"
    return {"status":status,"label":STATUS_LABELS[status],"why":record.get("why",""),"header":header,"lines":lines,"disputes":open_ones,"settled":settled,"counts":counts}


def _written_state(cell,sources,states,cells,settled,prefix=""):
    """What one target cell shows. A dispute stays on it; an agreement only while it holds the value the reads compared,
    and never on what a person typed, which no read compared."""
    if DISPUTED in (states.get(f) for f in sources if f):return DISPUTED
    own=sources[0];state=states.get(own)
    if own is None or state not in (CONFIRMED,FILLED,SETTLED) or _blank(cell.get("value")):return None
    if any(x.get("kind")=="owner_entry" for x in cell.get("evidence") or []):return None
    key=prefix+own
    compared=settled[key]["value"] if key in settled else cells[own]["value"]
    return state if same(own,compared,cell["value"]) else None


def written(readers,check,rules):
    """The states of the cells the rules view writes, keyed by target cell: one answer for the screen and the workbook."""
    record=(readers or {}).get("ai_check") or {}
    settled=check.get("settled") or {}
    fields=rules.get("fields") or {}
    header={k:s for k,sources in TARGET_HEADER.items() if k in fields
            and (s:=_written_state(fields[k],sources,check["header"],record.get("header",{}),settled))}
    lines=[]
    for line in rules.get("lines") or []:
        n=line["line"]
        states=check["lines"][n-1] if n<=len(check["lines"]) else {}
        cells=record["lines"][n-1] if n<=len(record.get("lines",[])) else {}
        lines.append({k:s for k,sources in TARGET_LINES.items() if k in line["cells"]
                      and (s:=_written_state(line["cells"][k],sources,states,cells,settled,f"line {n} "))})
    return {"header":header,"lines":lines}


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
    AI read cannot teach a wrong rule. Disputes cannot reach here unsettled: they block the export. A line's fills are
    looked for on every row, by value, since a row added, removed or moved takes them to another position."""
    check=view(readers,invoice)
    out={**invoice,**{f:None for f,s in check["header"].items() if s==FILLED},"lines":[dict(x) for x in invoice.get("lines") or []]}
    fills=[(f,c["value"]) for row in ((readers or {}).get("ai_check") or {}).get("lines",[]) for f,c in row.items() if c["state"]==FILLED]
    for n,row in enumerate(out["lines"]):
        states=check["lines"][n] if n<len(check["lines"]) else {}
        for f,value in fills:
            if states.get(f) not in (CONFIRMED,SETTLED) and not _blank(row.get(f)) and same(f,value,row[f]):row[f]=None
    return out
