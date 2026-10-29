"""Reader-side guards for scanned or image-only invoices (no usable text layer).

A scan is read by local OCR and, when that read is weak, by the AI from the page image. Nothing here
looks at reference data: the item master lives with the rules (fine_rules.match_line).

- Barcodes are stored as digits only. ``Line.gtin`` holds only codes that pass the GS1 mod-10 check
  digit (EAN-8, UPC-A/EAN-12, EAN-13, GTIN-14); a complete printed code that fails it goes to
  ``Line.barcode_unchecked`` with gtin None, so a lost field fails safe (empty, flagged).
- An AI barcode on a scan fills gtin only when the same complete digit run is printed on the same
  page in the local OCR text, as a standalone token, and no more lines claim it than the OCR saw,
  and not when every printed copy sits on a row that shows another line's values and none of its own.
- A weak local read is replaced by the AI read only when the AI read passes its own checks: printed
  line nets that sum to its net and the key header fields present.
"""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

GTIN_LENGTHS=(8,12,13,14)
UNREADABLE="barcode unreadable or incomplete"
MISPRINT="printed barcode fails the GTIN check digit (likely misprint) - check the printout"
UNCONFIRMED="barcode not confirmed on the page"
OTHER_ROW="barcode printed on another line's row"
# Machine codes for a line barcode review, set next to the reason text.
BARCODE_CODES={MISPRINT:"misprint",UNREADABLE:"unreadable",UNCONFIRMED:"unconfirmed",OTHER_ROW:"unconfirmed"}
AI_PAGE_IMAGE="AI (page image)"
KEY_HEADER=("number","date","po","net","tax")
NO_TAX="no tax total printed on the invoice"
TAX_NOT_READ="a tax label is printed but its total was not read"
# Machine codes for an empty tax total, set on the field review next to the reason text.
TAX_ABSENT_CODE="not_printed"
TAX_UNREAD_CODE="not_read"
INCLUSIVE_VAT="VAT printed as rate/(100+rate) of net"
# A digit run inside a token: spaces or hyphens between digits are print separators, not boundaries.
_SEPARATED=re.compile(r"(?<=\d)[ \-]+(?=\d)")
# A VAT/tax amount label. Not a tax id or registration, and not the 'Tax Invoice' document title.
_TAX_LABEL=re.compile(r"(?i)\b(?:vat|gst|hst|sales\s+tax|tax(?:\s+(?:amount|total))?|total\s+tax)\b"
                      r"(?!\s*(?:invoice|reg(?:istration|istered|\.)?|no\b|number|id\b|#|trn|pin)|\s*[-:]?\s*\d{6,})")
_TAX_ID=re.compile(r"(?i)\btrn\b")
_PO_BOX=re.compile(r"(?i)\bp\.?\s*o\.?\s*box\b")


def gtin_valid(digits):
    """GS1 mod-10 check digit on an 8, 12, 13 or 14 digit code."""
    if not isinstance(digits,str) or not digits.isdigit() or len(digits) not in GTIN_LENGTHS:return False
    body=[int(c) for c in digits[:-1]][::-1]
    total=sum(d*(3 if n%2==0 else 1) for n,d in enumerate(body))
    return (10-total%10)%10==int(digits[-1])


def barcode_digits(value):
    """The printed code as digits: a text prefix ('EAN:', 'UPC') and print separators removed.

    None when nothing is printed or the code is not a clean digit run (a gap, '?', or letters inside).
    """
    if value in (None,""):return None
    text=re.sub(r"^[^0-9]*","",str(value).strip())
    text=_SEPARATED.sub("",text).strip()
    return text if text.isdigit() else None


def classify_barcode(value):
    """(gtin, barcode_unchecked, reason) for one printed barcode value."""
    if value in (None,""):return None,None,None
    digits=barcode_digits(value)
    if digits is None or len(digits)<min(GTIN_LENGTHS) or len(digits)>max(GTIN_LENGTHS):
        return None,None,UNREADABLE
    if gtin_valid(digits):return digits,None,None
    return None,digits,MISPRINT


def is_scan(native_text_chars,path):
    """No usable text layer: an image, or a PDF whose own text is under 40 characters."""
    if path.suffix.lower() in (".png",".jpg",".jpeg",".tif",".tiff",".webp",".bmp"):return True
    return path.suffix.lower()==".pdf" and native_text_chars is not None and native_text_chars<40


def page_occurrences(boxes):
    """{page: Counter(digit run)} of standalone digit runs in the OCR tokens of each page."""
    words=[b for b in boxes or [] if b.get("geometry")=="word"]
    # One granularity only: word boxes when the OCR returned them, otherwise its text lines.
    tokens=words or [b for b in boxes or [] if b.get("geometry")!="word"]
    found={}
    for box in tokens:
        page=box.get("page") or 1
        text=_SEPARATED.sub("",str(box.get("text") or ""))
        found.setdefault(page,Counter()).update(re.findall(r"\d+",text))
    return found


def tokens_per_page(boxes):
    return dict(sorted(Counter(b.get("page") or 1 for b in boxes or []).items()))


def corroborate(claims,occurrences):
    """Which AI barcode claims the page OCR confirms.

    ``claims`` is a list of (index, page or None, digits). A claim is confirmed when its digits occur on
    its page (anywhere in the document when page is None) and the claims on that budget do not exceed the
    occurrences. Page-P and page-None claims share one budget per digit run. When claims exceed
    occurrences, every claimant of that run goes unconfirmed: line order cannot tell the owner apart.
    """
    pages=set(occurrences)
    doc_total=Counter()
    for counter in occurrences.values():doc_total.update(counter)
    by_run={}
    for index,page,digits in claims:by_run.setdefault(digits,[]).append((index,page))
    confirmed=set()
    for digits,group in by_run.items():
        if doc_total[digits]==0:continue
        if len(group)>doc_total[digits]:continue
        per_page=Counter(page for _,page in group if page is not None)
        if any(page not in pages or per_page[page]>occurrences[page][digits] for page in per_page):continue
        confirmed.update(index for index,_ in group)
    return confirmed


def _centre(box):
    return (float(box["box"][1])+float(box["box"][3]))/2


def _line_marks(line):
    """What identifies a line on its printed row: its amounts (at cents) and its description words."""
    marks=set()
    for field in ("net_amount","price"):
        value=_amount(line.get(field))
        if value is not None and value:marks|={f"{value:.2f}",f"{value:,.2f}"}
    marks|={w for w in re.findall(r"[a-z]{4,}",str(line.get("description") or "").lower())}
    return marks


def _row_marks(text):
    text=str(text or "").lower()
    return set(re.findall(r"(?<![\d.,])\d[\d,]*\.\d{2}(?![\d])",text))|set(re.findall(r"[a-z]{4,}",text))


def on_other_row(index,page,digits,lines,boxes):
    """True when every printed copy of the AI's code sits on a row that shows more of another line's values
    (amounts, description words) than of this line's: the code was read off a neighbour's row (K1).
    Without measured boxes, False."""
    words=[b for b in boxes or [] if b.get("geometry")=="word"]
    tokens=[b for b in (words or [b for b in boxes or [] if b.get("geometry")!="word"])
            if isinstance(b.get("box"),(list,tuple)) and len(b["box"])==4 and (page is None or (b.get("page") or 1)==page)]
    hits=[b for b in tokens if digits in re.findall(r"\d+",_SEPARATED.sub("",str(b.get("text") or "")))]
    if not hits:return False
    own=_line_marks(lines[index])
    others=[_line_marks(line) for n,line in enumerate(lines) if n!=index]
    for hit in hits:
        height=max(1.0,float(hit["box"][3])-float(hit["box"][1]))
        band=" ".join(str(b.get("text") or "") for b in tokens
                      if (b.get("page") or 1)==(hit.get("page") or 1) and abs(_centre(b)-_centre(hit))<=height*0.7)
        printed=_row_marks(band)
        if len(printed&own)>=max((len(printed&marks) for marks in others),default=0):return False
    return True


def _amount(value):
    try:return None if value in (None,"") else Decimal(str(value))
    except (InvalidOperation,ValueError):return None


def _unit(net):
    """One unit of the net's printed precision, at least cents."""
    return Decimal(1).scaleb(-max(2,-net.as_tuple().exponent))


def lines_reconcile(lines,net):
    """Printed line nets all present and summing to the net within one unit of its precision."""
    net=_amount(net)
    if net is None or not lines:return False
    amounts=[_amount(line.get("net_amount")) for line in lines]
    if any(a is None for a in amounts):return False
    return abs(sum(amounts,Decimal(0))-net)<=_unit(net)


def header_count(invoice):
    return sum(invoice.get(field) not in (None,"") for field in KEY_HEADER)


def weak_read(invoice):
    """The reasons a read cannot stand alone; empty when it can.

    Item identity is a printed sku or a checked gtin; an unchecked barcode is not identity.
    """
    if not invoice:return ["nothing read"]
    lines=invoice.get("lines") or []
    reasons=[]
    if not lines or not any(line.get("sku") or line.get("gtin") for line in lines):reasons.append("no line has an item code")
    if invoice.get("net") in (None,""):reasons.append("no net total")
    elif not lines_reconcile(lines,invoice.get("net")):reasons.append("line nets do not reconcile with the printed net")
    if header_count(invoice)<3:reasons.append("fewer than 3 of number, date, PO, net and tax")
    return reasons


def ai_self_check(invoice):
    """The reasons an AI read fails its own checks: printed line nets to its own net, key header fields."""
    if not invoice:return ["nothing read"]
    lines=invoice.get("lines") or []
    reasons=[]
    if not lines:reasons.append("no lines")
    if invoice.get("number") in (None,"") or invoice.get("net") in (None,""):reasons.append("no number or net")
    elif not lines_reconcile(lines,invoice.get("net")):reasons.append("printed line nets do not sum to its net")
    if header_count(invoice)<3:reasons.append("fewer than 3 of number, date, PO, net and tax")
    return reasons


def po_from_box(po,quotes,text):
    """True when the PO value is a P.O. Box: its quote names a box, or the page prints 'P.O. Box <po>'."""
    if po in (None,""):return False
    if any(_PO_BOX.search(str(q or "")) for q in quotes):return True
    digits=re.escape(str(po).strip())
    return bool(re.search(r"(?i)\bp\.?\s*o\.?\s*box\b[\s:#.]*"+digits+r"(?!\d)",text or ""))


def tax_label_printed(text):
    """True when the page text prints a VAT or tax amount label (not a tax id, not the 'Tax Invoice' title)."""
    return any(not _TAX_ID.search(line) and _TAX_LABEL.search(line) for line in str(text or "").splitlines())


def missing_tax_reason(text):
    """Why tax is empty: nothing printed, or a printed label the reader did not read."""
    return TAX_NOT_READ if tax_label_printed(text) else NO_TAX


def missing_tax_code(text):
    return TAX_UNREAD_CODE if tax_label_printed(text) else TAX_ABSENT_CODE


def inclusive_vat_lines(lines):
    """How many lines print VAT as rate/(100+rate) of their net, and not as rate/100 of it."""
    count=0
    for line in lines or []:
        net,tax=_amount(line.get("net_amount")),_amount(line.get("tax_amount"))
        if net is None or tax is None or net<=0 or tax<=0 or tax>=net:continue
        cent=Decimal("0.01")
        exclusive=any(abs(net*rate/100-tax)<=cent for rate in range(1,31))
        inclusive=any(abs(net*rate/(100+rate)-tax)<=cent for rate in range(1,31))
        if inclusive and not exclusive:count+=1
    return count
