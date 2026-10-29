from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
from .models import Invoice, Policy
from .references import decimal as D


def strip_ult(value):
    """R-004: Item Master ITEM carries a ``ULT_`` prefix; only that prefix is removed."""
    value=str(value or "").strip()
    return value[4:] if value[:4].upper()=="ULT_" else value


def key(i):
    return "|".join([i.seller or "", i.buyer or "", (i.number or "").strip().upper()])


def enrich(i: Invoice, refs):
    """Derive internal fields from a unique PO, preserving every existing value."""
    i=i.model_copy(deep=True); provenance=[]
    orders=[p for p in refs.get("orders",[]) if str(p["id"])==i.po]
    if len(orders)==1:
        p=orders[0]
        for field in ("site","buyer","location","origin","market","currency"):
            if not getattr(i,field):
                setattr(i,field,str(p[field])); provenance.append({"field":field,"value":str(p[field]),"source":f"PO {p['id']}","reference_version":refs.get("version")})
        sites=[s for s in refs.get("sites",[]) if str(s["id"])==i.site]
        if len(sites)==1 and not i.seller:
            i.seller=str(sites[0]["seller"])
            provenance.append({"field":"seller","value":i.seller,"source":f"Site {i.site}","reference_version":refs.get("version")})
    if not i.taxCode:
        rules=[t for t in refs.get("taxRules",[]) if all(str(t[k])==getattr(i,k) for k in ("origin","market","currency"))]
        if len(rules)==1:
            i.taxCode=rules[0]["code"]
            provenance.append({"field":"taxCode","value":i.taxCode,"source":"Unique approved tax mapping","reference_version":refs.get("version")})
    return i,provenance


def validate(i: Invoice, refs, policy: Policy, ledger=(), reviewed=False):
    issues=[]; matches=[]
    def fail(code,message,owner="Accounts payable",line=None):
        issues.append({"code":code,"message":message,"owner":owner,"line":line})
    for f in ("number","seller","site","buyer","po","location","date","currency","origin","market","taxCode"):
        if not getattr(i,f): fail("MISSING",f"Missing {f}")
    try: date.fromisoformat(i.date or "")
    except ValueError: fail("DATE","A valid YYYY-MM-DD invoice date is required")
    decimals=policy.currency_decimals.get(i.currency)
    if decimals is None or not isinstance(decimals,int) or not 0<=decimals<=4:
        fail("CURRENCY","Configure the currency's decimal places"); decimals=2
    rounded=lambda x:x.quantize(Decimal(1).scaleb(-decimals),rounding=ROUND_HALF_UP)
    if any(x["invoice_key"]==key(i) for x in ledger): fail("DUPLICATE","Invoice already exported for this seller and buying company")
    sites=[s for s in refs.get("sites",[]) if str(s["id"])==i.site and str(s["seller"])==i.seller]
    if len(sites)!=1: fail("SITE","Supplier site and invoicing legal entity do not match","Supplier operations")
    elif i.supplier_name:
        normalized=lambda x:re.sub(r"[^\w]","",x.casefold())
        names=[sites[0]["name"],*sites[0].get("aliases",[])]
        if normalized(i.supplier_name) not in {normalized(x) for x in names}:
            fail("SUPPLIER_NAME","Invoice supplier name differs from the site's approved name or aliases","Supplier operations")
    if not any(all(str(r[k])==getattr(i,k) for k in ("site","buyer","origin","market","currency")) and r["status"]=="approved" for r in refs.get("routes",[])):
        fail("ROUTE","Trading relationship is not approved for this seller site, buyer, origin, market and currency","Supplier operations")
    orders=[p for p in refs.get("orders",[]) if str(p["id"])==i.po]
    p=orders[0] if len(orders)==1 else None
    if not p: fail("PO","No unique purchase order found","Buyer")
    else:
        if p.get("location_type") not in ("Store (S)","Warehouse (W)"):
            fail("LOCATION_TYPE","PO reference must provide an approved store/warehouse location type","Reference owner")
        if p["status"]!="open": fail("PO_STATUS","Purchase order is not open","Buyer")
        for f in ("site","buyer","location","currency","origin","market"):
            if str(p[f])!=getattr(i,f): fail("PO_SCOPE",f"PO {f} differs from invoice","Buyer")
    if not i.lines: fail("LINES","No invoice line items were read")
    used=defaultdict(lambda:Decimal(0)); within=defaultdict(lambda:Decimal(0)); net=Decimal(0)
    for e in ledger:
        for a in e["allocations"]: used[a["key"]]+=D(a["qty"])
    for n,l in enumerate(i.lines,1):
        candidates=[x for x in refs.get("items",[]) if str(x["site"])==i.site and (
            (l.item_id and str(x["id"])==l.item_id) or
            (not l.item_id and ((l.gtin and strip_ult(x["gtin"])==strip_ult(l.gtin)) or (not l.gtin and l.sku and str(x["sku"])==l.sku))))]
        item=candidates[0] if len(candidates)==1 else None
        if not item: fail("ITEM","No unique exact approved item / supplier SKU / barcode match","Item steward",n)
        if item and l.item_id and ((l.sku and str(item["sku"])!=l.sku) or (l.gtin and strip_ult(item["gtin"])!=strip_ult(l.gtin))):
            fail("ITEM_CONFLICT","The confirmed internal item conflicts with the supplier SKU or barcode","Item steward",n)
        if item and l.gtin and l.sku and str(item["sku"])!=l.sku: fail("ITEM_CONFLICT","Barcode and supplier SKU identify different items","Item steward",n)
        if l.qty is None or l.qty<=0: fail("QTY","Quantity is missing or not positive",line=n)
        if l.price is None or l.price<0: fail("PRICE","Net unit price is missing or negative",line=n)
        if l.qty is not None and l.price is not None: net+=rounded(l.qty*l.price)
        pls=[x for x in (p or {}).get("lines",[]) if item and str(x["item"])==str(item["id"])]
        if len(pls)!=1:
            fail("PO_LINE","Cannot select one PO line for this item","Buyer",n); continue
        pl=pls[0]; allocation=f"{p['id']}|{pl['id']}"
        if l.uom!=pl["uom"]: fail("UOM","Unit differs; an approved conversion is required","Buyer",n)
        if l.price is not None and abs(l.price-D(pl["price"]))>policy.price_tolerance: fail("PRICE","Unit price differs from the PO beyond tolerance","Buyer",n)
        received=sum((D(r["qty"]) for r in refs.get("receipts",[]) if str(r["po"])==str(p["id"]) and str(r["line"])==str(pl["id"]) and r["status"]=="accepted"),Decimal(0))
        available=received-D(pl["invoiced"])-used[allocation]
        within[allocation]+=l.qty or Decimal(0)
        if within[allocation]>available: fail("RECEIPT",f"Needs {within[allocation]} units; {available} accepted received units remain","Receiving",n)
        if within[allocation]>D(pl["ordered"])-D(pl["invoiced"])-used[allocation]: fail("ORDER_QTY","Exceeds remaining ordered quantity","Buyer",n)
        matches.append({"line":n,"item":str(item["id"]),"gtin":strip_ult(l.gtin or item.get("gtin","")),"po_price":str(pl["price"]),"available":str(available),"allocation":{"key":allocation,"qty":str(l.qty or 0)}})
    if i.net is None or abs(rounded(net)-i.net)>policy.total_tolerance: fail("TOTAL",f"Calculated net {rounded(net)} differs from the invoice net")
    taxes=[t for t in refs.get("taxRules",[]) if all(str(t[k])==getattr(i,k) for k in ("origin","market","currency")) and t["code"]==i.taxCode]
    if len(taxes)!=1: fail("TAX","No unique approved tax mapping","Tax reviewer")
    elif i.tax is None or abs(rounded(net*D(taxes[0]["rate"]))-i.tax)>policy.total_tolerance: fail("TAX_TOTAL","Tax differs from the configured rule","Tax reviewer")
    if not reviewed: fail("REVIEW","Compare values and derived references with the document, then confirm review")
    return {"ready":not issues,"issues":issues,"matches":matches,"net":str(rounded(net)),"reference_version":refs.get("version"),"location_type":p.get("location_type") if p else None}


# --------------------------------------------------------------------------- fine-rules result view
# Production review fields, banner and download read ONE fine_rules.run_invoice
# result through rules_view. A value is shown only with its evidence: an owner
# sheet row, an owner mapping-table key, or the text printed on the invoice.
# Anything else stays empty and is flagged; nothing is inferred or defaulted.

ITEM_THRESHOLD=Decimal("0.95")
# (review field, label, result header name). Seller entity, Buyer code and Origin are not in the
# owner's target Header and no owner sheet holds them, so they are not shown (owner form extra_fields).
RULES_FIELDS=(("number","Invoice number","Document"),("site","Supplier site","Supplier Site"),
    ("po","Purchase order","Order No"),("location","Delivery location","Location"),
    ("location_type","Location type","Location Type"),("date","Invoice date","Document Date"),
    ("currency","Currency","Currency"),("market","Market","Market"),("taxCode","Tax code","Tax Code"),
    ("net","Net total","Net Amount"),("tax","Tax total","Tax Amount"))
LINE_FIELDS=("Item","UPC","Unit Cost","Quantity","Unit Tax Code")
_SHEET_ROW=re.compile(r"[^!,|]+!\d+")


def _blank(value):return value is None or str(value).strip()==""


def _evidence(entry):
    reference=str(entry.get("reference") or "")
    source=str(entry.get("source") or "")
    if entry.get("evidence_kind")=="owner_rule":kind="owner_rule"
    elif entry.get("evidence_kind")=="printed":kind="printed"
    # Picked by the rules among candidate orders (decision 16): never shown as printed.
    elif entry.get("evidence_kind")=="selected":kind="selected"
    # The owner's supplier-code pick (rule OWNER-PICK): scored as an owner entry, never as machine-read.
    elif entry.get("evidence_kind")=="owner_entry":kind="owner_entry"
    elif _SHEET_ROW.match(reference):kind="sheet"
    elif "|" in reference:kind="table"
    elif source.startswith("Invoice"):kind="printed"
    else:kind="rule"
    return {"kind":kind,"source":source,"reference":reference,"original":str(entry.get("original") or ""),
            "rule":str(entry.get("rule") or ""),"confidence":str(entry.get("confidence") or ""),
            # A value the rules derived after the owner's supplier-code pick: kept for the owner-assisted sub-count.
            **({"assisted_by":str(entry["assisted_by"])} if entry.get("assisted_by") else {})}


def _amount_forms(value):
    try:d=Decimal(str(value))
    except Exception:return set()
    forms={str(d),f"{d:,}",f"{d:.2f}",f"{d:,.2f}"}
    return {f for f in forms if f and f not in ("0","0.00")} or {"0.00"}


def printed_evidence(value,text="",boxes=()):
    """Locate a printed amount on the document; return its evidence or None."""
    if _blank(value):return None
    forms=_amount_forms(value)
    pattern=lambda f:re.compile(r"(?<![\d.,])"+re.escape(f)+r"(?![\d]|[.,]\d)")
    for box in boxes or ():
        t=str(box.get("text") or "")
        if any(pattern(f).search(t) for f in forms):
            where=f"page {box.get('page',1)}"+(f" box {list(box['box'])}" if isinstance(box.get("box"),(list,tuple)) else "")
            return {"kind":"printed","source":"Printed on invoice","reference":where,"original":t[:120],"rule":"","confidence":"Exact"}
    for n,page in enumerate(str(text or "").split("\f"),1):
        for f in forms:
            m=pattern(f).search(page)
            if m:return {"kind":"printed","source":"Printed on invoice","reference":f"page {n} text","original":page[max(0,m.start()-40):m.end()+10].strip()[:120],"rule":"","confidence":"Exact"}
    return None


# Header fields read off the document; extraction evidence (job.evidence) may stand in when lineage has none.
PRINTED_KEYS=("number","date","currency","net","tax")


def extraction_evidence(entry):
    """job.evidence entry {quote, page, box?, source} -> a printed evidence item, or None."""
    if not isinstance(entry,dict) or not str(entry.get("quote") or "").strip():return None
    page=entry.get("page")
    where=(f"page {page}" if page else "page not given")+(f" box {list(entry['box'])}" if isinstance(entry.get("box"),(list,tuple)) else "")
    return {"kind":"printed","source":f"Invoice ({entry.get('source') or 'ai'})","reference":where,"original":str(entry["quote"])[:120],
            "rule":"","confidence":""}


# Cells the target template cannot be written without; an empty one is the only thing a confirm cannot clear.
REQUIRED_HEADER=("number","site","po","location","location_type","date","net","tax","taxCode")
REQUIRED_LINE=("Item","Unit Cost","Quantity","Unit Tax Code")
_ID=re.compile(r"[1-9]\d{0,14}")
_ENTRY_FORMAT={"site":"id","po":"id","location":"id","Item":"id","location_type":"location_type","date":"date",
               "net":"amount","tax":"amount","Unit Cost":"amount","Quantity":"amount"}


def owner_entries(raw):
    """Check reviewer-entered target values; blank entries are dropped, a malformed one is refused."""
    raw=raw or {};out={"header":{},"lines":{}}
    def check(name,value):
        value=str(value).strip();kind=_ENTRY_FORMAT.get(name,"text")
        # Before any pattern looks at it (RULE 8).
        if len(value)>64:raise ValueError(f"{name} is too long")
        if kind=="id" and not _ID.fullmatch(value):raise ValueError(f"{name} must be a whole number")
        if kind=="location_type" and value not in ("Store (S)","Warehouse (W)"):raise ValueError("Location type must be Store (S) or Warehouse (W)")
        if kind=="date":
            try:date.fromisoformat(value)
            except ValueError:raise ValueError("Invoice date must be YYYY-MM-DD") from None
        if kind=="amount":
            try:Decimal(value)
            except Exception:raise ValueError(f"{name} must be a number") from None
        return value
    for k,v in (raw.get("header") or {}).items():
        if k not in REQUIRED_HEADER:raise ValueError(f"{k} cannot be entered by the reviewer")
        if not _blank(v):out["header"][k]=check(k,v)
    for n,row in (raw.get("lines") or {}).items():
        if not str(n).isdigit():raise ValueError("Line entries are keyed by line number")
        for k,v in (row or {}).items():
            if k not in REQUIRED_LINE+("UPC",):raise ValueError(f"{k} cannot be entered by the reviewer")
            if not _blank(v):out["lines"].setdefault(str(int(n)),{})[k]=check(k,v)
    return out


def _entered(value,rules_value):
    return {"kind":"owner_entry","source":"Entered by reviewer","reference":"review",
            "original":"" if _blank(rules_value) else f"rules value {rules_value} had no evidence","rule":"","confidence":"Reviewer"}


def rules_view(result,text="",boxes=(),printed=None,entries=None):
    """Turn one run_invoice result into evidence-checked review fields, lines and banner issues.

    ``entries`` are reviewer-entered values (owner_entries); they fill only cells the rules left empty or unevidenced.
    """
    header=result.get("header") or {}; lineage=result.get("lineage") or []
    entries=entries or {};typed=entries.get("header") or {};typed_lines=entries.get("lines") or {}
    found=defaultdict(list)
    for x in lineage:found[(x.get("target"),x.get("line") or None)].append(_evidence(x))
    lines=result.get("lines") or []
    flags=[]
    def field(name,value,evidence,label,line=None,entry=None,reason=None):
        if not _blank(entry) and (_blank(value) or not evidence):
            return {"value":str(entry),"evidence":[_entered(entry,value)],"flagged":False,"reason":""}
        if _blank(value):
            flags.append((label,line,reason or "not found in the owner's sheets or printed on the invoice"))
            return {"value":None,"evidence":[],"flagged":True,"reason":reason or "Not found"}
        if not evidence:
            flags.append((label,line,"has no evidence and is left empty"))
            return {"value":None,"evidence":[],"flagged":True,"reason":"No evidence"}
        return {"value":str(value),"evidence":evidence,"flagged":False,"reason":""}
    out_lines=[]
    for n,row in enumerate(lines,1):
        cells={}
        for c in LINE_FIELDS:
            entry=(typed_lines.get(str(n)) or {}).get(c)
            if c=="Unit Tax Code" and _blank(entry):entry=typed.get("taxCode")
            cells[c]=field(c,row.get(c),found.get((c,n),[]),c,n,entry,(row.get("Field Reasons") or {}).get(c))
        out_lines.append({"line":n,"cells":cells,"status":row.get("Validation Status") or "","source_row":row.get("Source Row") or "",
                          "match_method":row.get("Match Method") or ""})
    fields={}
    for key_,label,name in RULES_FIELDS:
        if name=="Tax Code":
            # One code per invoice: the lines' Unit Tax Code, with the lines' own evidence.
            codes={l["cells"]["Unit Tax Code"]["value"] for l in out_lines}
            value=next(iter(codes)) if len(codes)==1 else None
            evidence=[e for l in out_lines[:1] for e in l["cells"]["Unit Tax Code"]["evidence"]]
        else:
            value=header.get(name);evidence=found.get((name,None),[])
            if not evidence and key_ in PRINTED_KEYS:
                located=extraction_evidence(((printed or {}).get("header") or {}).get(key_))
                evidence=[located] if located else []
            if not evidence and name in ("Net Amount","Tax Amount"):
                located=printed_evidence(value,text,boxes);evidence=[located] if located else []
        fields[key_]={"label":label,"target":name,**field(key_,value,evidence,label,entry=typed.get(key_))}
    # Shown on review only (not a target column); unconfigured means blank, which is not an invoice problem.
    if not _blank(header.get("Buyer Name")):
        fields["buyer_name"]={"label":"Buyer name","target":"Buyer Name",
                              **field("buyer_name",header["Buyer Name"],found.get(("Buyer Name",None),[]),"Buyer name")}
    check=result.get("item_resolution") or result.get("item_line_check")
    if isinstance(check,dict) and check.get("total") is not None:
        resolved,total=int(check.get("resolved") or 0),int(check.get("total") or 0)
        definition=str(check.get("definition") or "")
    else:
        resolved=sum(1 for l in out_lines if l["cells"]["Item"]["value"]);total=len(out_lines)
        definition="Lines whose Item has an owner Item Master row"
    rate=(Decimal(resolved)/Decimal(total)) if total else None
    below=bool(check.get("below")) if isinstance(check,dict) and "below" in check else (rate is None or rate<ITEM_THRESHOLD)
    issues=[]
    for e in result.get("exceptions") or []:
        issues.append({"code":str(e.get("Exception Type") or "Exception"),"message":str(e.get("Description") or ""),
                       "owner":str(e.get("Owner") or ""),"line":e.get("Line No.") or None,"rule":str(e.get("Rule ID") or ""),
                       "evidence":str(e.get("Candidates / Evidence") or ""),"blocking":bool(e.get("blocking",True)),
                       "level":"review" if e.get("blocking",True) else "warning",
                       "check":str(e.get("Check ID") or ""),"type":str(e.get("Engine Type") or "")})
    # RULES raises its own ITEM-LINE-95 exception when lines exist; add ours only when it did not.
    if below and not any(i["rule"]=="ITEM-LINE-95" for i in issues):
        issues.append({"code":"Owner Review","message":f"Item lines resolved {resolved}/{total}: below 95%, owner review required",
                       "owner":"Owner","line":None,"rule":"ITEM-95","evidence":"","blocking":True,"level":"review"})
    # A second reader (OCR) disagreeing with the extracted value never edits it; it is surfaced for the reviewer.
    printed=printed if isinstance(printed,dict) else {}
    disagreements=[(None,k,e) for k,e in (printed.get("header") or {}).items()]
    disagreements+=[(n,k,e) for n,row in enumerate(printed.get("lines") or [],1) if isinstance(row,dict) for k,e in row.items()]
    for line,k,e in disagreements:
        if isinstance(e,dict) and isinstance(e.get("review"),dict):
            issues.append({"code":"Evidence Disagreement","message":f"{k}: {e['review'].get('reason') or 'readers disagree'}; value kept, check the document",
                           "owner":"Accounts payable","line":line,"rule":"EVID-OCR","evidence":f"page {e.get('page') or 1}","blocking":False,"level":"warning"})
    # Decision 83 (POG-010): a date check only flags; it never blocks, fills or changes the status.
    receipt=result.get("receipt_date") or {}
    if receipt.get("flag"):
        issues.append({"code":"Date Check","message":f"{receipt['flag']}; check the order and receipt dates",
                       "owner":"Buyer","line":None,"rule":"POG-010","evidence":str(receipt.get("reference") or ""),
                       "blocking":False,"level":"warning"})
    for label,line,why in flags:
        key_=next((k for k,lab,_ in RULES_FIELDS if lab==label),label)
        required=label in REQUIRED_LINE if line is not None else key_ in REQUIRED_HEADER
        hint="; enter it on the review screen if you have it" if required else ""
        issues.append({"code":"Missing Evidence","message":f"{label} {why}{hint}","owner":"Accounts payable","line":line,
                       "rule":"EVIDENCE","evidence":"","blocking":required,"level":"hard" if required else "warning"})
    return {"status":result.get("status") or "Review","fields":fields,"lines":out_lines,"issues":issues,
            "item_lines":{"resolved":resolved,"total":total,"rate":str(rate.quantize(Decimal("0.0001"))) if rate is not None else None,
                          "threshold":str(ITEM_THRESHOLD),"owner_review":below,"definition":definition},
            "po_candidates":int(result.get("po_candidates") or 0),"config_version":result.get("config_version"),
            # Decision 76: Order Date is evidence only (review note and a Checks row), never an editable field.
            **({"order_date":result["order_date"]} if result.get("order_date") else {}),
            **({"receipt_date":result["receipt_date"]} if result.get("receipt_date") else {}),
            # Codes left when the rules cannot pick a supplier code: offered for the owner's pick, never a value.
            **({"supplier_site_candidates":result["supplier_site_candidates"]} if result.get("supplier_site_candidates") else {})}


def rules_key(view):
    f=view["fields"]
    return "|".join(["rules",f["site"]["value"] or "",(f["number"]["value"] or "").strip().upper()])


def accepted_ids(view):
    """Review-level issues a reviewer's confirm accepts; a change here needs a fresh confirm."""
    return sorted({(i.get("rule") or "",i.get("code") or "",str(i.get("line") or "")) for i in (view or {}).get("issues",[])
                   if i.get("level","review")=="review" and i.get("blocking")})


def rules_validation(view,ledger=(),reviewed=False):
    """Banner and readiness from the rules view only.

    Hard issues (duplicate, rules not run, an empty required target cell) hold the invoice whatever the reviewer does.
    Review-level issues, ITEM-LINE-95 and every other rules exception included, hold it until a reviewer confirms;
    the confirm is the final say and the accepted issues travel in the export receipt.
    """
    if not view:
        return {"ready":False,"issues":[{"code":"Rules Pending","message":"The fine rules have not run on this revision yet","owner":"","line":None,
                                         "blocking":True,"level":"hard"}],
                "matches":[],"source":"fine_rules","accepted":[]}
    issues=[]
    for issue in view["issues"]:
        issue={"level":"review" if issue.get("blocking") else "warning",**issue}
        if reviewed and issue["level"]=="review" and issue["blocking"]:issue.update(blocking=False,accepted=True)
        issues.append(issue)
    if view["fields"]["number"]["value"] and any(x.get("invoice_key")==rules_key(view) for x in ledger):
        issues.append({"code":"Duplicate","message":"Invoice already exported for this supplier site","owner":"Accounts payable","line":None,
                       "blocking":True,"level":"hard"})
    if not reviewed:
        issues.append({"code":"REVIEW","message":"Compare values and evidence with the document, then confirm review","owner":"","line":None,
                       "blocking":True,"level":"review"})
    ready=not any(i["blocking"] for i in issues)
    matches=[{"line":l["line"],"item":l["cells"]["Item"]["value"] or "","gtin":l["cells"]["UPC"]["value"] or ""} for l in view["lines"]]
    accepted=[{"rule":i.get("rule",""),"code":i["code"],"line":i.get("line")} for i in issues if i.get("accepted")]
    return {"ready":ready,"issues":issues,"matches":matches,"source":"fine_rules","status":view["status"],"accepted":accepted,
            "item_lines":view["item_lines"],"location_type":view["fields"]["location_type"]["value"]}
