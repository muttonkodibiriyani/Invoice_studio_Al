import io
import re
from datetime import date
from decimal import Decimal
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from .matching import strip_ult

HEADERS={
"Header":["Transaction Number","Document","Supplier Site","Order No","Location","Location Type","Document Date","Total Cost Ex Tax","Tax Amount","Ref No. 1","Ref No. 2","Ref No. 3","Comment"],
"Tax_Breakdown":["Transaction Number","Tax Code","Tax Basis"],
"Details":["Transaction Number","Item","UPC","Unit Cost","Quantity","Unit Tax Code"]}


def text_cell(cell,value):
    # Explicit string typing prevents user-controlled formula injection.
    cell.value=str(value) if value is not None else ""
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
    dv=DataValidation(type="list",formula1='"Store (S),Warehouse (W)"');s.add_data_validation(dv)
    for transaction,(invoice,result) in enumerate(entries,1):
        s=w["Header"];row=transaction+1
        loc=invoice.location or ""
        loc_type=result.get("location_type")
        if loc_type not in ("Store (S)","Warehouse (W)"):raise ValueError("Reference location type must be Store (S) or Warehouse (W)")
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
_NUMBER_ID=re.compile(r"[1-9]\d{0,14}")


def _id_cell(cell,value):
    # Owner examples hold Supplier Site, Order No, Location and Item as numbers.
    if _NUMBER_ID.fullmatch(str(value)):cell.value=int(value)
    else:text_cell(cell,value)


def rules_workbook(views,upc="empty",draft=False):
    """Target template (Header / Tax_Breakdown / Details) from fine-rules views only.

    UPC stays empty by default (owner answer: Details.Item = Item Master ITEM_PARENT is what populates);
    "barcode" writes the invoice barcode as text.

    Every written cell comes from a view value that carries evidence; the
    returned map records that evidence per transaction and cell. Ref No./Comment stay truly empty.

    ``draft``: a review copy of the same workbook for invoices nobody approved yet. Where the export would refuse
    a cell (flagged, no value or no evidence), the cell stays empty; nothing is ever filled to look complete.
    """
    if upc not in UPC_MODES:raise ValueError("UPC mode must be barcode or empty")
    # Approved by the rules, or confirmed by a reviewer (owner_accepted lists the review issues they accepted).
    if not views or not draft and any(v["status"]!="Approved" and "owner_accepted" not in v for v in views):
        raise ValueError("Only invoices the fine rules approved or a reviewer confirmed can be exported")
    w=Workbook();w.remove(w.active);cells={}
    for name,cols in HEADERS.items():
        s=w.create_sheet(name);s.append(cols);s.freeze_panes="A2"
        for c in s[1]:c.font=Font(bold=True,color="FFFFFF");c.fill=PatternFill("solid",fgColor="173D3B")
        for col in s.columns:s.column_dimensions[col[0].column_letter].width=max(18,len(str(col[0].value))+2)
    dv=DataValidation(type="list",formula1='"Store (S),Warehouse (W)"');w["Header"].add_data_validation(dv)
    def need(f,where):
        if f["flagged"] or f["value"] is None or not f["evidence"]:
            if draft:return None
            raise ValueError(f"{where} has no evidenced value")
        return f["value"]
    for transaction,v in enumerate(views,1):
        f=v["fields"];s=w["Header"];row=transaction+1
        ev=cells.setdefault(transaction,{})
        def put(sheet,r,c,field):
            if not draft or w[sheet].cell(r,c).value not in (None,""):ev[f"{sheet}!{w[sheet].cell(r,c).coordinate}"]=field["evidence"]
        s.cell(row,1,transaction)
        text_cell(s.cell(row,2),need(f["number"],"Document"));put("Header",row,2,f["number"])
        for c,k in ((3,"site"),(4,"po"),(5,"location")):
            _id_cell(s.cell(row,c),need(f[k],f[k]["label"]));put("Header",row,c,f[k])
        loc_type=need(f["location_type"],"Location Type")
        if loc_type not in ("Store (S)","Warehouse (W)"):
            if not draft:raise ValueError("Location Type must be Store (S) or Warehouse (W)")
            loc_type=None
        text_cell(s.cell(row,6),loc_type);dv.add(s.cell(row,6));put("Header",row,6,f["location_type"])
        when=need(f["date"],"Document Date")
        s.cell(row,7,date.fromisoformat(when) if when else None).number_format="m/d/yyyy";put("Header",row,7,f["date"])
        net,tax=(None if x is None else Decimal(x) for x in (need(f["net"],"Net total"),need(f["tax"],"Tax total")))
        s.cell(row,8,net);put("Header",row,8,f["net"]);s.cell(row,9,tax);put("Header",row,9,f["tax"])
        t=w["Tax_Breakdown"];t.cell(row,1,transaction);text_cell(t.cell(row,2),need(f["taxCode"],"Tax code"))
        t.cell(row,3,net);put("Tax_Breakdown",row,2,f["taxCode"]);put("Tax_Breakdown",row,3,f["net"])
        d=w["Details"]
        for line in v["lines"]:
            c=line["cells"];n=d.max_row+1;d.cell(n,1,transaction)
            _id_cell(d.cell(n,2),need(c["Item"],f"Line {line['line']} Item"));put("Details",n,2,c["Item"])
            if upc=="barcode" and c["UPC"]["value"]:
                text_cell(d.cell(n,3),c["UPC"]["value"]);d.cell(n,3).number_format="@";put("Details",n,3,c["UPC"])
            for col,k in ((4,"Unit Cost"),(5,"Quantity")):
                x=need(c[k],f"Line {line['line']} {k}");d.cell(n,col,None if x is None else Decimal(x));put("Details",n,col,c[k])
            text_cell(d.cell(n,6),need(c["Unit Tax Code"],f"Line {line['line']} Unit Tax Code"));put("Details",n,6,c["Unit Tax Code"])
    b=io.BytesIO();w.save(b);return b.getvalue(),cells
