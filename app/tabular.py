"""Small, bounded OpenDocument reader that preserves identifiers as text."""
import io
import zipfile
from defusedxml import ElementTree

TABLE="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
TEXT="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
OFFICE="urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def ods_tables(content):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        if sum(i.file_size for i in z.infolist())>30_000_000:raise ValueError("Expanded ODS exceeds 30 MB")
        root=ElementTree.fromstring(z.read("content.xml"))
    tables={}
    for sheet in root.findall(f".//{{{TABLE}}}table"):
        name=sheet.get(f"{{{TABLE}}}name","");rows=[]
        for row in sheet.findall(f"{{{TABLE}}}table-row"):
            cells=[]
            for cell in row:
                if cell.tag not in (f"{{{TABLE}}}table-cell",f"{{{TABLE}}}covered-table-cell"):continue
                if cell.get(f"{{{TABLE}}}formula"):raise ValueError(f"{name}: formulas are not accepted as reference evidence")
                display="\n".join("".join(p.itertext()) for p in cell.findall(f".//{{{TEXT}}}p"))
                kind=cell.get(f"{{{OFFICE}}}value-type")
                value=cell.get(f"{{{OFFICE}}}value",display) if kind in ("float","currency","percentage") else cell.get(f"{{{OFFICE}}}date-value",display) if kind=="date" else display
                repeat=int(cell.get(f"{{{TABLE}}}number-columns-repeated","1"))
                if len(cells)+repeat>80:
                    if value:raise ValueError("ODS table exceeds 80 columns")
                    repeat=max(0,80-len(cells))
                cells.extend([value]*repeat)
            while cells and not cells[-1]:cells.pop()
            repeats=int(row.get(f"{{{TABLE}}}number-rows-repeated","1"))
            if not cells:continue
            if len(rows)+repeats>50000:raise ValueError("ODS table exceeds 50,000 rows")
            rows.extend([cells[:]]*repeats)
        if name in tables:raise ValueError("Duplicate ODS worksheet names")
        tables[name]=rows
    return tables
