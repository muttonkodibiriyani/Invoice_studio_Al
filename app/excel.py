import io
import re
from datetime import date
from decimal import Decimal
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from .matching import strip_ult
from .location_type import STORE, WAREHOUSE, written
from .models import UNWRITABLE_TEXT

HEADERS={
"Header":["Transaction Number","Document","Supplier Site","Order No","Location","Location Type","Document Date","Total Cost Ex Tax","Tax Amount","Ref No. 1","Ref No. 2","Ref No. 3","Comment"],
"Tax_Breakdown":["Transaction Number","Tax Code","Tax Basis"],
"Details":["Transaction Number","Item","UPC","Unit Cost","Quantity","Unit Tax Code"]}


def text_cell(cell,value):
    # Explicit string typing prevents user-controlled formula injection.
    cell.value=UNWRITABLE_TEXT.sub("",str(value)) if value is not None else ""
    cell.data_type="s"


def workbook(invoice,result):
    return batch_workbook([(invoice,result)])


def batch_workbook(entries):
    if not entries or any(not r["ready"] for _,r in entries):
        raise ValueError("Only validated invoices can be exported")
    w=Workbook(); w.remove(w.active)
    for name,cols in HEADERS.items():
        s=w.create_sheet(name); s.append(cols); s.freeze_panes="A2"
        for c in s[1]: c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="173D3B")
        for col in s.columns: s.column_dimensions[col[0].column_letter].width=max(18,len(str(col[0].value))+2)
    s=w["Header"]
    dv=DataValidation(type="list",formula1=f'"{STORE},{WAREHOUSE}"');s.add_data_validation(dv)
    for transaction,(invoice,result) in enumerate(entries,1):
        s=w["Header"];row=transaction+1
        loc=invoice.location or ""
        loc_type=written(result.get("location_type"))
        if not loc_type:raise ValueError(f"Reference location type must be {STORE} or {WAREHOUSE}")
        vals=[transaction,invoice.number,invoice.site,invoice.po,loc,loc_type,date.fromisoformat(invoice.date),invoice.net,invoice.tax,"","","",""]
        for c,v in enumerate(vals,1):
            if c in (3,4,5) and re.fullmatch(r"[1-9]\d{0,14}",str(v)):s.cell(row,c,int(v))
            elif c in (2,3,4,5,6,10,11,12,13):text_cell(s.cell(row,c),v)
            else:s.cell(row,c,v)
        s.cell(row,7).number_format="m/d/yyyy";dv.add(s.cell(row,6))
        t=w["Tax_Breakdown"];t.append([transaction,None,invoice.net]);text_cell(t.cell(row,2),invoice.taxCode)
        for l,m in zip(invoice.lines,result["matches"]):
            s=w["Details"];n=s.max_row+1
            s.append([transaction,None,None,l.price,l.qty,None])
            for c,v in ((2,m["item"]),(3,strip_ult(m["gtin"])),(6,invoice.taxCode)):
                if c==2 and re.fullmatch(r"[1-9]\d{0,14}",str(v)):s.cell(n,c,int(v))
                else:text_cell(s.cell(n,c),v)
            s.cell(n,3).number_format="@"
            for c in (4,5):s.cell(n,c).number_format="0.0000"
    b=io.BytesIO();w.save(b);return b.getvalue()


UPC_MODES=("barcode","empty")
# What a cell shows of the AI check (app/ai_check): a fill and a note, no column added to the template. A cell with
# neither was not confirmed. Column A of the Header row says it for the whole invoice.
MARKS={"confirmed":("C6EFCE","AI confirmed: the local read and the AI read agree. This does not prove the value right."),
       "filled":("BDD7EE","AI filled: the local read was blank and the value is the AI's. Check it against the document."),
       "disputed":("F8CBAD","In dispute: the local read and the AI read differ."),
       "settled":("FFE699","A person settled a dispute between the local read and the AI read here."),
       "not_confirmed":("D9D9D9","Not AI-confirmed")}
_INVOICE_MARKS={"confirmed":"confirmed","partial":"filled","disputed":"disputed","not_confirmed":"not_confirmed"}


def _mark(cell,state,text=""):
    color,words=MARKS[state]
    cell.fill=PatternFill("solid",fgColor=color)
    cell.comment=Comment(f"{words} {text}".strip(),"Invoice Studio",width=320,height=120)


def _mark_invoice(sheet,row,check):
    """The invoice's AI check on its Transaction Number cell, which always holds a value."""
    counts=check.get("counts") or {}
    said=check.get("why") or ", ".join(f"{n} {name}" for name,n in counts.items() if n)
    cell=sheet.cell(row,1);cell.fill=PatternFill("solid",fgColor=MARKS[_INVOICE_MARKS[check["status"]]][0])
    cell.comment=Comment(f"{check['label']}: {said}.","Invoice Studio",width=320,height=120)


def _mark_cell(cell,states,fields,check,line=None):
    """The strongest of ``fields``' states on this cell: its own field, or a value derived from them. A derived cell
    takes only a dispute, since the AI did not read the code derived from what it read."""
    own=fields[0]
    for field in fields:
        if states.get(field)=="disputed":
            found=next((x for x in check["disputes"] if x["field"]==field and x["line"]==line),None)
            _mark(cell,"disputed",f"Local: {found['local']}. AI: {found['ai']}." if found else "");return
    if own and states.get(own) in MARKS:_mark(cell,states[own])
_NUMBER_ID=re.compile(r"[1-9]\d{0,14}")


def _id_cell(cell,value):
    # Owner examples hold Supplier Site, Order No, Location and Item as numbers.
    if _NUMBER_ID.fullmatch(str(value)):cell.value=int(value)
    else:text_cell(cell,value)


def rules_workbook(views,upc="empty",blanks=False):
    w,cells=_rules_sheets(views,upc,blanks)
    b=io.BytesIO();w.save(b);return b.getvalue(),cells


def blank_fields(view,upc="empty"):
    """The cells the real export leaves empty for one invoice, counted on the sheets the writer fills. Ref No./Comment
    and, in the default mode, UPC are empty on every invoice by owner rule, so they are not gaps."""
    w,_=_rules_sheets([view],upc,True)
    by_rule={"Ref No. 1","Ref No. 2","Ref No. 3","Comment"}|({"UPC"} if upc=="empty" else set())
    return sum(not str("" if cell.value is None else cell.value).strip() for s in w.worksheets
               for name,*cells in s.iter_cols() if name.value not in by_rule for cell in cells)


def _rules_sheets(views,upc,blanks):
    """Target template (Header / Tax_Breakdown / Details) from fine-rules views only.

    UPC stays empty by default (owner answer: Details.Item = Item Master ITEM_PARENT is what populates);
    "barcode" writes the invoice barcode as text.

    Every written cell comes from a view value that carries evidence; the
    returned map records that evidence per transaction and cell. Ref No./Comment stay truly empty.

    ``blanks``: a cell with no evidenced value (flagged, no value or no evidence) stays empty instead of refusing the
    workbook; nothing is ever filled to look complete. The draft and, since owner decision D11, the real export.
    """
    if upc not in UPC_MODES:raise ValueError("UPC mode must be barcode or empty")
    # Approved by the rules, or confirmed by a reviewer (owner_accepted lists the review issues they accepted).
    if not views or not blanks and any(v["status"]!="Approved" and "owner_accepted" not in v for v in views):
        raise ValueError("Only invoices the fine rules approved or a reviewer confirmed can be exported")
    w=Workbook();w.remove(w.active);cells={}
    for name,cols in HEADERS.items():
        s=w.create_sheet(name);s.append(cols);s.freeze_panes="A2"
        for c in s[1]:c.font=Font(bold=True,color="FFFFFF");c.fill=PatternFill("solid",fgColor="173D3B")
        for col in s.columns:s.column_dimensions[col[0].column_letter].width=max(18,len(str(col[0].value))+2)
    dv=DataValidation(type="list",formula1=f'"{STORE},{WAREHOUSE}"');w["Header"].add_data_validation(dv)
    def need(f,where):
        if f["flagged"] or f["value"] is None or not f["evidence"]:
            if blanks:return None
            raise ValueError(f"{where} has no evidenced value")
        return f["value"]
    for transaction,v in enumerate(views,1):
        f=v["fields"];s=w["Header"];row=transaction+1
        check=v.get("ai_check") or {"status":"not_confirmed","label":"Not AI-confirmed","why":"no AI read was made","header":{},"lines":[],"disputes":[]}
        ev=cells.setdefault(transaction,{})
        def put(sheet,r,c,field):
            if not blanks or w[sheet].cell(r,c).value not in (None,""):ev[f"{sheet}!{w[sheet].cell(r,c).coordinate}"]=field["evidence"]
        s.cell(row,1,transaction)
        text_cell(s.cell(row,2),need(f["number"],"Document"));put("Header",row,2,f["number"])
        for c,k in ((3,"site"),(4,"po"),(5,"location")):
            _id_cell(s.cell(row,c),need(f[k],f[k]["label"]));put("Header",row,c,f[k])
        loc_type=written(need(f["location_type"],"Location Type"))
        if not loc_type and not blanks:raise ValueError(f"Location Type must be {STORE} or {WAREHOUSE}")
        text_cell(s.cell(row,6),loc_type);dv.add(s.cell(row,6));put("Header",row,6,f["location_type"])
        when=need(f["date"],"Document Date")
        s.cell(row,7,date.fromisoformat(when) if when else None).number_format="m/d/yyyy";put("Header",row,7,f["date"])
        net,tax=(None if x is None else Decimal(x) for x in (need(f["net"],"Net total"),need(f["tax"],"Tax total")))
        s.cell(row,8,net).number_format="0.0000";put("Header",row,8,f["net"]);s.cell(row,9,tax).number_format="0.0000";put("Header",row,9,f["tax"])
        t=w["Tax_Breakdown"];t.cell(row,1,transaction);text_cell(t.cell(row,2),need(f["taxCode"],"Tax code"))
        t.cell(row,3,net).number_format="0.0000";put("Tax_Breakdown",row,2,f["taxCode"]);put("Tax_Breakdown",row,3,f["net"])
        d=w["Details"]
        for line in v["lines"]:
            c=line["cells"];n=d.max_row+1;d.cell(n,1,transaction)
            _id_cell(d.cell(n,2),need(c["Item"],f"Line {line['line']} Item"));put("Details",n,2,c["Item"])
            if upc=="barcode" and c["UPC"]["value"]:
                text_cell(d.cell(n,3),c["UPC"]["value"]);d.cell(n,3).number_format="@";put("Details",n,3,c["UPC"])
            for col,k in ((4,"Unit Cost"),(5,"Quantity")):
                x=need(c[k],f"Line {line['line']} {k}");d.cell(n,col,None if x is None else Decimal(x)).number_format="0.0000";put("Details",n,col,c[k])
            text_cell(d.cell(n,6),need(c["Unit Tax Code"],f"Line {line['line']} Unit Tax Code"));put("Details",n,6,c["Unit Tax Code"])
            states=check["lines"][line["line"]-1] if 0<line["line"]<=len(check["lines"]) else {}
            for col,fields in ((2,("item_id","sku","gtin")),(3,("gtin",)),(4,("price",)),(5,("qty",))):
                if col!=3 or d.cell(n,3).value not in (None,""):_mark_cell(d.cell(n,col),states,fields,check,line["line"])
        for cell,fields in ((s.cell(row,2),("number",)),(s.cell(row,3),(None,"supplier_name")),(s.cell(row,4),("po",)),(s.cell(row,5),(None,"buyer_name")),
                            (s.cell(row,7),("date",)),(s.cell(row,8),("net",)),(s.cell(row,9),("tax",)),(t.cell(row,3),("net",))):
            _mark_cell(cell,check["header"],fields,check)
        _mark_invoice(s,row,check)
    return w,cells
