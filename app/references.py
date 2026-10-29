import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from openpyxl import load_workbook, Workbook

TABLES = {
    "Suppliers": ("sites", ["id", "seller", "name", "country"]),
    "Routes": ("routes", ["site", "buyer", "origin", "market", "currency", "status"]),
    "Items": ("items", ["id", "site", "sku", "gtin", "uom"]),
    "POs": ("orders", ["id", "site", "buyer", "origin", "market", "currency", "location", "location_type", "status"]),
    "POLines": ("po_lines", ["po", "id", "item", "ordered", "invoiced", "price", "uom"]),
    "Receipts": ("receipts", ["id", "po", "line", "qty", "status"]),
    "TaxRules": ("taxRules", ["origin", "market", "currency", "code", "rate"]),
}


def scalar(value):
    return not isinstance(value, bool) and isinstance(value, (str, int, float))


def decimal(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError("Non-finite number")
        return d
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid numeric reference value: {str(value)[:40]}") from None


def validate_references(data):
    if not isinstance(data, dict):
        raise ValueError("Reference data must be an object")
    for _, (key, columns) in TABLES.items():
        if key == "po_lines":
            continue
        if not isinstance(data.get(key), list):
            raise ValueError(f"Missing reference table: {key}")
        for row in data[key]:
            if not isinstance(row, dict) or any(k not in row for k in columns):
                raise ValueError(f"{key}: required columns are {', '.join(columns)}")
            if any(not scalar(row[k]) for k in columns):
                raise ValueError(f"{key}: required values must be strings or numbers")
    for site in data["sites"]:
        aliases=site.get("aliases",[])
        if not isinstance(aliases,list) or any(not isinstance(alias,str) or not alias.strip() for alias in aliases):
            raise ValueError("Supplier aliases must be a list of non-empty strings")
    unique = lambda rows, keys: len(rows) == len({tuple(str(r.get(k)) for k in keys) for r in rows})
    for key, keys in [("sites",["id"]),("orders",["id"]),("receipts",["id"]),
                      ("routes",["site","buyer","origin","market","currency"]),
                      ("items",["site","sku"]),("taxRules",["origin","market","currency","code"])]:
        if not unique(data[key], keys):
            raise ValueError(f"Duplicate keys in {key}")
    sites = {str(s["id"]) for s in data["sites"]}
    for row in data["sites"]:
        if not all(row[k] for k in ("id","seller","name","country")):
            raise ValueError("Supplier identity cannot be blank")
    for row in data["items"] + data["routes"] + data["orders"]:
        if str(row["site"]) not in sites:
            raise ValueError("Unknown supplier site in references")
    pairs = set()
    for order in data["orders"]:
        if order["location_type"] not in ("Store (S)","Warehouse (W)"):
            raise ValueError("PO location_type must be Store (S) or Warehouse (W)")
        lines=order.get("lines")
        if not isinstance(lines,list) or not lines:
            raise ValueError("PO lines must be a non-empty list")
        if any(not isinstance(line,dict) for line in lines):
            raise ValueError("PO lines must contain objects")
        if not unique(lines, ["id"]):
            raise ValueError("PO lines are missing or duplicated")
        for line in lines:
            if any(k not in line for k in ("id","item","ordered","invoiced","price","uom")):
                raise ValueError("PO line columns are incomplete")
            if any(not scalar(line[k]) for k in ("id","item","ordered","invoiced","price","uom")):
                raise ValueError("PO line values must be strings or numbers")
            if any(decimal(line[k]) < 0 for k in ("ordered","invoiced","price")):
                raise ValueError("PO numbers cannot be negative")
            if not any(str(i["id"])==str(line["item"]) and str(i["site"])==str(order["site"]) for i in data["items"]):
                raise ValueError("PO line item is absent from that supplier site")
            pairs.add((str(order["id"]),str(line["id"])))
    for row in data["receipts"]:
        if (str(row["po"]),str(row["line"])) not in pairs:
            raise ValueError("Receipt references an unknown PO line")
        decimal(row["qty"])  # Negative approved receipts represent returns.
        if row["status"] not in ("accepted","pending","rejected"):
            raise ValueError("Receipt status must be accepted, pending or rejected")
    for row in data["taxRules"]:
        if not 0 <= decimal(row["rate"]) <= 1:
            raise ValueError("Tax rates must be fractions between 0 and 1")
    return data


def import_references(content: bytes, filename: str):
    if filename.lower().endswith(".json"):
        data = json.loads(content)
    elif filename.lower().endswith(".ods"):
        from .tabular import ods_tables
        tables=ods_tables(content)
        if set(("Header","Tax_Breakdown","Details")).issubset(tables):
            raise ValueError("This is a target invoice workbook, not an item-master / PO / receipt reference file. Keep it as an output example and upload the reference data separately.")
        data={}
        for title,(key,columns) in TABLES.items():
            rows=tables.get(title)
            if not rows:raise ValueError(f"Missing reference worksheet {title}. Use the downloaded reference template.")
            header=[str(x).strip() for x in rows[0]]
            if not set(columns).issubset(header) or len(set(header))!=len(header):raise ValueError(f"{title}: required headers missing or duplicated")
            data[key]=[{k:str(row[n]).strip() if n<len(row) else "" for n,k in enumerate(header)} for row in rows[1:]]
        lines=data.pop("po_lines")
        for p in data["orders"]:p["lines"]=[{k:v for k,v in x.items() if k!="po"} for x in lines if x["po"]==p["id"]]
    elif filename.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                expanded=sum(i.file_size for i in z.infolist())
        except (zipfile.BadZipFile, OSError, RuntimeError):
            raise ValueError("Reference workbook is damaged or is not a valid .xlsx file") from None
        if expanded > 30_000_000:
            raise ValueError("Expanded workbook is too large")
        try:
            w = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
        except Exception:
            raise ValueError("Reference workbook is damaged or is not a valid .xlsx file") from None
        try:
            if set(("Header","Tax_Breakdown","Details")).issubset(w.sheetnames):
                raise ValueError("This is a target invoice workbook, not a master-data reference workbook. Upload the item-master and PO/receipt data separately.")
            data = {}
            for title, (key, columns) in TABLES.items():
                if title not in w:
                    raise ValueError(f"Missing worksheet {title}. Download the reference template.")
                sheet = w[title]
                if sheet.max_row > 50000 or sheet.max_column > 40:
                    raise ValueError("Reference worksheet exceeds the row/column limit")
                rows = sheet.iter_rows()
                first=next(rows,None)
                if first is None:
                    raise ValueError(f"{title}: worksheet is empty")
                header = [str(c.value).strip() if c.value is not None else "" for c in first]
                if not set(columns).issubset(header) or len(set(header)) != len(header):
                    raise ValueError(f"{title}: headers must include {', '.join(columns)} without duplicates")
                records = []
                for cells in rows:
                    if any(c.data_type == "f" for c in cells):
                        raise ValueError(f"{title}: formulas are not accepted in reference data")
                    row = {k: str(c.value).strip() if c.value is not None else "" for k,c in zip(header,cells)}
                    if any(row.values()):
                        records.append(row)
                data[key] = records
            lines = data.pop("po_lines")
            for p in data["orders"]:
                p["lines"] = [{k:v for k,v in x.items() if k != "po"} for x in lines if x["po"]==p["id"]]
        finally:
            w.close()
    else:
        raise ValueError("Upload a reference .xlsx / .ods workbook or canonical .json snapshot")
    validate_references(data)
    data["version"] = hashlib.sha256(content).hexdigest()[:16]
    data["imported_at"] = datetime.now(timezone.utc).isoformat()
    return data


def reference_workbook(data):
    w=Workbook(); w.remove(w.active)
    for title,(key,cols) in TABLES.items():
        s=w.create_sheet(title); s.append(cols)
        rows=data.get(key,[])
        if key=="po_lines":
            rows=[{"po":p["id"],**l} for p in data.get("orders",[]) for l in p["lines"]]
        for r in rows:
            s.append([str(r.get(k,"")) for k in cols])
        s.freeze_panes="A2"
        for col in s.columns:
            s.column_dimensions[col[0].column_letter].width=22
    b=io.BytesIO(); w.save(b); return b.getvalue()
