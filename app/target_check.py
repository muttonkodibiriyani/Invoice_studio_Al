"""Read-only accuracy check of one invoice's target-sheet rows (Header / Tax_Breakdown / Details).

Every cell the target workbook would hold gets exactly one status, MEASURE's per-cell bucket, after its
evidence was re-read at the source it points to (owner sheet row from the loaded lookup tables, the stored
printed text, the owner mapping config, or an attributed owner entry):

- verified: filled, has evidence, and the source holds the value (owner entries carry sub 'owner_entry');
- empty_owner_rule: empty because the owner said it stays empty (Details UPC in the default mode, Header Ref No. 1-3
  and Comment; form 01a10c4d). Correct, but n/a: its own group, never added to verified (Dispatcher ruling);
- empty_flagged: empty, with the reason: a real gap;
- mismatch: the evidence does not hold the value;
- over_cited: some cited rows hold the value, others do not;
- no_evidence: filled with no evidence;
- unverifiable: evidence not re-checkable (e.g. a printed source and no stored text);
- data_gap: rests on a malformed owner extract row;
- owner_entry_unattributed: an owner entry without a matching stored entry, actor and time;
- missing_line: a Details cell of an item line the invoice prints but the read lacks (or merged away); estimated
  when lines_to_net fails, never matched (MEASURE, T2(b)).

A filled owner-rule cell is a template failure (mismatch). The owner-facing line collapses the buckets:
verified · empty by owner rule · empty (flagged) · needs checking (every other bucket).
The definitions mirror MEASURE's release metric (bench target_metric.check) so the in-app counts and the
gate agree: the gate scores the sheet as written, i.e. the rules view the workbook is built from.
Deterministic: no AI or cloud call; only the loaded owner tables, the stored text and the owner config are read.
Private config values (buyer_name) are compared in memory only. Arithmetic, join and template checks run beside the cell statuses. Nothing here changes a value, and the
module never logs values: callers log counts and field names only.
"""

import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

VERIFIED, EMPTY, MISMATCH = "verified", "empty_flagged", "mismatch"
NO_EVIDENCE, UNVERIFIABLE, DATA_GAP, OVER_CITED = "no_evidence", "unverifiable", "data_gap", "over_cited"
OWNER_RULE_EMPTY, UNATTRIBUTED, OWNER_ENTRY = "empty_owner_rule", "owner_entry_unattributed", "owner_entry"
MISSING_LINE = "missing_line"
STATUSES = (VERIFIED, OWNER_RULE_EMPTY, EMPTY, MISMATCH, OVER_CITED, NO_EVIDENCE, UNVERIFIABLE, DATA_GAP, UNATTRIBUTED,
            MISSING_LINE)
NEEDS_CHECKING = "needs_checking"
# Empty by owner rule is correct but proves nothing from evidence: its own group, not added to verified (n/a).
GROUPS = (VERIFIED, OWNER_RULE_EMPTY, EMPTY, NEEDS_CHECKING)
PASS, FAIL, SKIPPED, WARNING = "pass", "fail", "skipped", "warning"

# The owner's target template (owner form 01a10c4d: 13 Header columns, Ref No. 1-3 and Comment empty).
TEMPLATE = {
    "Header": ["Transaction Number", "Document", "Supplier Site", "Order No", "Location", "Location Type",
               "Document Date", "Total Cost Ex Tax", "Tax Amount", "Ref No. 1", "Ref No. 2", "Ref No. 3", "Comment"],
    "Tax_Breakdown": ["Transaction Number", "Tax Code", "Tax Basis"],
    "Details": ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code"],
}
# Header column -> rules view field key. Transaction Number and the owner-empty columns are structural.
HEADER_FIELDS = {"Document": "number", "Supplier Site": "site", "Order No": "po", "Location": "location",
                 "Location Type": "location_type", "Document Date": "date", "Total Cost Ex Tax": "net",
                 "Tax Amount": "tax"}
OWNER_EMPTY = {"Ref No. 1", "Ref No. 2", "Ref No. 3", "Comment"}
OWNER_EMPTY_REASON = "Owner rule: left empty (owner form 01a10c4d)"
UPC_EMPTY_REASON = "Owner rule: UPC left empty; Item is the Item Master ITEM_PARENT (owner form 01a10c4d)"
MISSING_LINE_REASON = "Item line missing or merged: the read lines do not sum to the Header net"
LOCATION_TYPES = ("Store (S)", "Warehouse (W)")
# ISO 4217 minor units for the currencies the owner trades in; anything else is compared at two decimals.
CURRENCY_DECIMALS = {"KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "IQD": 3, "TND": 3, "LYD": 3}
DEFAULT_DECIMALS = 2


# --------------------------------------------------------------------------- evidence (mirrors MEASURE)


def _norm(value):
    return re.sub(r"\s+", "", str(value)).casefold()


def _dec(value):
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _blank(value):
    return value is None or str(value).strip() == ""


def shifted(data):
    """A 'PO Extract' row whose columns are shifted: CREATED_DATE is not a date or RMS_ITEM_ID not 9 digits."""
    created, item = str(data.get("CREATED_DATE") or ""), str(data.get("RMS_ITEM_ID") or "")
    return not re.match(r"^\d{4}-\d{2}-\d{2}", created) or not re.fullmatch(r"\d{9}", item)


def printed(text, original):
    """True when the original is in the job text (numbers by value), None when the job has no text or its
    text checks are unavailable. ``text`` is a TextChecks (or plain text, indexed here)."""
    text = TextChecks.of(text)
    if not text.has_text:
        return None
    original_norm = _norm(original)
    if original_norm and original_norm in text.norm:
        return True
    amount = _dec(original)
    return amount is not None and amount in text.amounts


DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%m-%y", "%d/%m/%y",
                "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d-%B-%Y", "%B %d %Y")
ORDINAL_DAY = re.compile(r"(?i)\b(\d{1,2})(st|nd|rd|th)\b")


def same(value, original):
    """Value equals the original: text, the ULT_ barcode prefix rule, number by value, or a parsed date."""
    if _norm(value) == _norm(original):
        return True
    if re.sub(r"(?i)^ult_", "", str(original).strip()) == str(value).strip():
        return True
    a, b = _dec(value), _dec(original)
    if a is not None and b is not None:
        return a == b
    printed = ORDINAL_DAY.sub(r"\1", str(original).strip())  # "3rd March 2031" reads as "3 March 2031"
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(printed, fmt).date().isoformat() == str(value)
        except ValueError:
            pass
    return False


class Sources:
    """Read-only access to what evidence points at.

    ``row(sheet, number)`` returns the owner row's data dict or None; ``config`` is the saved fine-rules
    mapping config (the dict the owner posted); ``text`` is the job's stored TextChecks. The app passes the stored
    ones; plain text is indexed here, which runs the patterns in this process (tests and the reader child only).
    """

    def __init__(self, row=None, config=None, text="", site_rows=None):
        self._row = row or (lambda sheet, number: None)
        self._site_rows = site_rows or (lambda site: [])
        self.config = config or {}
        self.text = TextChecks.of(text)
        self._cache = {}

    def row(self, ref):
        sheet, _, number = ref.strip().rpartition("!")
        number = number.split(" (+")[0]  # 'row (+N)': rows beyond the listed ones are not named
        if not number.isdigit():
            return None
        key = (sheet, int(number))
        if key not in self._cache:
            self._cache[key] = self._row(sheet, int(number))
        return self._cache[key]

    def site_rows(self, site):
        key = ("site", str(site))
        if key not in self._cache:
            self._cache[key] = list(self._site_rows(site))
        return self._cache[key]


def store_rows(store):
    """Row reader over the app's lookup store (lookup_rows, read-only)."""
    import json

    def read(sheet, number):
        with store.connection() as connection:
            found = connection.execute("SELECT payload FROM lookup_rows WHERE sheet=? AND row_number=?",
                                       (sheet, number)).fetchone()
        return (json.loads(found[0]).get("data") or {}) if found else None
    return read


def store_site_rows(store, limit=50):
    """Item Master rows of one supplier site (SUPPLIER equal to the site), read-only."""
    import json

    from app.reference_lookup import normalize_name

    def read(site):
        with store.connection() as connection:
            found = connection.execute(
                "SELECT r.payload FROM lookup_terms t JOIN lookup_rows r ON r.id=t.row_id "
                "WHERE t.kind='item' AND t.term=? LIMIT ?", ("f:site:" + normalize_name(site), limit)).fetchall()
        rows = [json.loads(x[0]).get("data") or {} for x in found]
        return [d for d in rows if str(d.get("SUPPLIER")) == str(site)]
    return read


SITE_DERIVATION = "accepted POGRN order EBS code and LOCATIONS entity"


def _held(rows, column, value):
    return [_norm(r.get(column, "")) == _norm(value) if column in r else any(_norm(v) == _norm(value) for v in r.values())
            for r in rows]


def check_evidence(value, evidence, sources, location=None):
    """One evidence record against its source -> (status, sub-bucket).

    Mirrors MEASURE (bench target_metric.check plus comb_score.verify rulings a-d). ``location`` is the
    invoice's Location value, used by evidence that rests on the owner's location master.
    """
    refs = [x for x in str(evidence.get("reference") or "").split(", ") if "!" in x]
    source = str(evidence.get("source") or "")
    master = (sources.config.get("location_master") or {}).get(str(location or "")) or {}
    if source == "location master":
        return (VERIFIED, "") if _norm(master.get("type", "")) == _norm(value) else (MISMATCH, "")
    if source == "Location-market mapping":
        return (VERIFIED, "") if _norm(master.get("market", "")) == _norm(value) else (MISMATCH, "")
    if source == "prefix fallback":
        return MISMATCH, UNVERIFIABLE  # no owner table row and no written owner rule for the 800/380 prefix
    if source.endswith(SITE_DERIVATION):
        # SUP-003: the order rows' EBS code + the location's entity/currency spell the site's Item Master
        # SUPPLIER_NAME, and the site is in the owner's supplier-site table.
        rows = [sources.row(x) for x in refs]
        if not rows or any(r is None for r in rows):
            return MISMATCH, ""
        if any(x.strip().startswith("PO Extract") and shifted(r) for x, r in zip(refs, rows)):
            return MISMATCH, DATA_GAP
        codes = {str(r.get("EBS_SUPPLIER_CODE", "")).upper() for r in rows}
        entity = str(master.get("entity_currency", "")).upper()
        names = {re.sub(r"\s+", "", str(r.get("SUPPLIER_NAME", ""))).upper() for r in sources.site_rows(value)}
        listed = any(x.get("supplier_site") == value for x in sources.config.get("supplier_sites", []))
        ok = listed and len(codes) == 1 and entity and any(n == next(iter(codes)) + entity for n in names)
        return (VERIFIED, "") if ok else (MISMATCH, "")
    if source == "Reviewed invoice tax code":
        source = "Invoice " + source  # a printed code: checked like printed text
    if refs:
        words = source.split()
        column = words[-1] if words and words[-1].isupper() else None
        if source.startswith(SELECTED):
            column = "RMS_ORDER_NO"  # decision 16: the cited POGRN rows must carry the selected order
        rows = [sources.row(x) for x in refs]
        if any(r is None for r in rows):
            return MISMATCH, ""
        if any(x.strip().startswith("PO Extract") and shifted(r) for x, r in zip(refs, rows)):
            return MISMATCH, DATA_GAP
        held = _held(rows, column, value)
        # over_cited: some cited rows hold the value, but the lineage also cites rows that do not.
        return (VERIFIED, "") if all(held) else (MISMATCH, OVER_CITED) if any(held) else (MISMATCH, "")
    if source == "Supplier-site table":
        site = str(evidence.get("reference") or "").split("|")[0]
        ok = any(s.get("supplier_site") == site and s.get("currency") == value
                 for s in sources.config.get("supplier_sites", []))
        return (VERIFIED, "") if ok else (MISMATCH, "")
    if source.startswith("Owner VAT code table"):
        ok = any(v.get("code") == value for v in sources.config.get("vat_codes", []))
        return (VERIFIED, "") if ok else (MISMATCH, "")
    if source.startswith("Invoice") or source == PRINTED_SOURCE:
        original = evidence.get("original", "")
        found = printed(sources.text, original)
        if found is False and same(value, original):
            found = printed(sources.text, value)  # reader-added prefix (ULT_): the value itself is printed
        if found is None:
            return MISMATCH, UNVERIFIABLE
        return (VERIFIED, "") if found and same(value, original) else (MISMATCH, "")
    return MISMATCH, ""  # evidence of a kind that is neither an owner sheet row, owner config nor printed text


# --------------------------------------------------------------------------- cells


def group(status):
    return status if status in (VERIFIED, OWNER_RULE_EMPTY, EMPTY) else NEEDS_CHECKING


def _cell(sheet, column, line, value, status, sub="", reason="", evidence=None, scope="metric"):
    ev = evidence or {}
    if status == MISMATCH and sub:
        status, sub = sub, ""  # the bucket is the status; sub only marks owner-entry verification
    return {"sheet": sheet, "column": column, "line": line, "value": "" if value is None else str(value),
            "status": status, "group": group(status), "sub": sub, "reason": reason, "scope": scope,
            "evidence": {k: str(ev.get(k) or "") for k in ("kind", "source", "reference", "original", "rule")}}


def _entry_key(line, key):
    return f"header:{key}" if line is None else f"line:{line}:{key}"


def _field_cell(sheet, column, line, field, sources, entries, attribution, entry_key, view=None):
    """One view field (value + evidence list) -> a cell with exactly one status."""
    value = (field or {}).get("value")
    evidence = list((field or {}).get("evidence") or [])
    if _blank(value):
        reason = (field or {}).get("reason") or "Not found"
        return _cell(sheet, column, line, None, EMPTY, reason=reason)
    if not evidence:
        return _cell(sheet, column, line, value, MISMATCH, NO_EVIDENCE, "Filled with no evidence")
    location = (((view or {}).get("fields") or {}).get("location") or {}).get("value")
    first = evidence[0]
    if first.get("kind") == "owner_entry":
        entered = _lookup_entry(entries, entry_key)
        who = (attribution or {}).get(_entry_key(*entry_key)) or {}
        if entered is not None and same(value, entered) and who.get("actor") and who.get("at"):
            return _cell(sheet, column, line, value, VERIFIED, OWNER_ENTRY, "Entered by the owner on review", first)
        return _cell(sheet, column, line, value, MISMATCH, UNATTRIBUTED,
                     "Owner entry without a matching stored entry, actor and time", first)
    status, sub = check_evidence(value, first, sources, location)
    located = None
    if status == VERIFIED and _ai_unlocated(first):
        # A short AI value (e.g. a quantity '2') matches the page text almost anywhere: without a box it is not located.
        status, sub, located = MISMATCH, UNVERIFIABLE, AI_UNLOCATED_REASON
    reason = {MISMATCH: "Evidence does not hold the value", UNVERIFIABLE: "evidence not re-checkable",
              DATA_GAP: "Source row malformed in the owner extract",
              OVER_CITED: "Some cited rows do not hold the value"}.get(sub or status, "")
    if located:
        reason = located
    elif sub == UNVERIFIABLE and _ai_scan(first):
        reason = SCAN_REASON
    elif status == VERIFIED and str(first.get("source") or "").startswith(SELECTED):
        reason = "Selected by POG-001 from the cited POGRN rows; not printed on the invoice"
    return _cell(sheet, column, line, value, status, sub, reason, first)


SELECTED = "Selected by POG-001"  # RULES 7ae7836: Order No picked among candidates (decision 16)
PRINTED_SOURCE = "Printed on invoice"  # matching.printed_evidence label (e.g. a multi-number totals row)
SCAN_REASON = "evidence not re-checkable: read by AI from a scan; no box to re-check"
AI_SOURCE = "Invoice (ai)"
AI_UNLOCATED_REASON = "AI value not located on the page"


def _ai_unlocated(evidence):
    """An AI read with no box: its quote may be found in the page text, but not at a located position."""
    return str(evidence.get("source") or "") == AI_SOURCE and " box " not in f"{evidence.get('reference') or ''} "


def _ai_scan(evidence):
    """Printed evidence with no text layer to re-read and no box on its page reference: an AI read of a scan."""
    source = str(evidence.get("source") or "")
    return ((source.startswith("Invoice") or source == PRINTED_SOURCE)
            and " box " not in f"{evidence.get('reference') or ''} ")


def _lookup_entry(entries, entry_key):
    line, key = entry_key
    entries = entries or {}
    if line is None:
        return (entries.get("header") or {}).get(key)
    return ((entries.get("lines") or {}).get(str(line)) or {}).get(key)


def sheet_cells(view, sources, transaction=1, upc="empty", entries=None, attribution=None):
    """Every cell the target workbook holds for this invoice, from the rules view it is written from.

    Cells in the gate's metric scope carry scope 'metric'; the structural cells (Transaction Number,
    Ref No. 1-3, Comment) carry scope 'sheet'.
    """
    fields, cells = view.get("fields") or {}, []
    for column in TEMPLATE["Header"]:
        if column == "Transaction Number":
            cells.append(_cell("Header", column, None, transaction, VERIFIED, reason="Join key",
                               evidence={"kind": "structure", "source": "Transaction number of this invoice"},
                               scope="sheet"))
        elif column in OWNER_EMPTY:
            cells.append(_cell("Header", column, None, None, OWNER_RULE_EMPTY, reason=OWNER_EMPTY_REASON, scope="sheet"))
        else:
            key = HEADER_FIELDS[column]
            cells.append(_field_cell("Header", column, None, fields.get(key), sources, entries, attribution,
                                     (None, key), view=view))
    cells.append(_cell("Tax_Breakdown", "Transaction Number", None, transaction, VERIFIED, reason="Join key",
                       evidence={"kind": "structure", "source": "Transaction number of this invoice"}, scope="sheet"))
    cells.append(_field_cell("Tax_Breakdown", "Tax Code", None, fields.get("taxCode"), sources, entries,
                             attribution, (None, "taxCode"), view=view))
    cells.append(_field_cell("Tax_Breakdown", "Tax Basis", None, fields.get("net"), sources, entries, attribution,
                             (None, "net"), view=view))
    for line in view.get("lines") or []:
        n, c = line.get("line"), line.get("cells") or {}
        cells.append(_cell("Details", "Transaction Number", n, transaction, VERIFIED, reason="Join key",
                           evidence={"kind": "structure", "source": "Transaction number of this invoice"},
                           scope="sheet"))
        for column in TEMPLATE["Details"][1:]:
            if column == "UPC" and upc == "empty":
                # The workbook leaves UPC empty whatever the rules found (owner answer).
                cells.append(_cell("Details", column, n, None, OWNER_RULE_EMPTY, reason=UPC_EMPTY_REASON))
                continue
            # A UPC mode scores UPC against its printed barcode evidence, like any other cell.
            cells.append(_field_cell("Details", column, n, c.get(column), sources, entries, attribution, (n, column),
                                     view=view))
    return cells


# --------------------------------------------------------------------------- arithmetic, joins, template


def decimals(currency):
    return CURRENCY_DECIMALS.get(str(currency or "").strip().upper(), DEFAULT_DECIMALS)


def _q(amount, places):
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _value(view, key):
    return _dec(((view.get("fields") or {}).get(key) or {}).get("value"))


def _line_value(line, column):
    return _dec(((line.get("cells") or {}).get(column) or {}).get("value"))


def _vat_rate(config, code):
    for row in (config or {}).get("vat_codes") or []:
        if str(row.get("code") or "").strip() == str(code or "").strip():
            return _dec(row.get("rate"))
    return None


def printed_line_count(text):
    """A single distinct 'Total lines: N' printed on the invoice (mirrors docling_extract._printed_line_count)."""
    values = {int(m.group(1)) for m in re.finditer(r"(?i)\btotal\s+lines\s*:?\s*(\d{1,4})\b", text or "")
              if 0 < int(m.group(1)) <= 1000}
    return next(iter(values)) if len(values) == 1 else None


AMOUNT = re.compile(r"-?\d[\d,]*\.?\d*")
NUMBER = re.compile(r"\d[\d,]*\.?\d*")
DISCOUNT_LABEL = re.compile(r"(?i)\bdiscount\b")


def _amounts(pattern, text):
    return {a for a in map(_dec, pattern.findall(str(text or ""))) if a is not None and a.is_finite()}


def text_checks(text):
    """What the checks need from the job text, as plain JSON. Runs the patterns over the text, so the app runs it
    in a reader child under the readers' limits (engines.text_checks), never in the API process."""
    text = str(text or "")
    if not text.strip():
        return {"has_text": False}
    return {"has_text": True, "norm": _norm(text),
            "amounts": sorted(map(str, _amounts(AMOUNT, text))),
            "numbers": sorted(map(str, _amounts(NUMBER, text))),
            "discounts": sorted(map(str, set().union(*(_amounts(NUMBER, row) for row in text.splitlines()
                                                       if DISCOUNT_LABEL.search(row))))),
            "line_count": printed_line_count(text)}


UNAVAILABLE_DETAIL = "Printed-text checks unavailable: the text check was stopped"


class TextChecks:
    """A job's stored text_checks: the printed-text checks compare against these and run no pattern over the text.
    ``unavailable`` names why they could not be built (the reader child was stopped or failed)."""

    def __init__(self, data=None):
        data = data or {}
        self.unavailable = data.get("unavailable")
        self.has_text = bool(data.get("has_text")) and not self.unavailable
        self.norm = str(data.get("norm") or "")
        self.amounts, self.numbers, self.discounts = ({Decimal(a) for a in data.get(key) or ()}
                                                      for key in ("amounts", "numbers", "discounts"))
        self.line_count = data.get("line_count")

    @classmethod
    def validated(cls, data):
        """The reader child's text_checks, accepted only as the structure text_checks returns: expected keys and
        types, numbers that parse; anything else raises ValueError."""
        if not isinstance(data, dict) or not isinstance(data.get("has_text"), bool):
            raise ValueError("text checks: unexpected structure")
        if not data["has_text"]:
            if set(data) != {"has_text"}:
                raise ValueError("text checks: unexpected keys")
            return {"has_text": False}
        if set(data) != {"has_text", "norm", "amounts", "numbers", "discounts", "line_count"}:
            raise ValueError("text checks: unexpected keys")
        count = data["line_count"]
        if not isinstance(data["norm"], str) or not (count is None or (type(count) is int and 0 < count <= 1000)):
            raise ValueError("text checks: unexpected types")
        out = {"has_text": True, "norm": data["norm"], "line_count": count}
        for key in ("amounts", "numbers", "discounts"):
            values = data[key]
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError("text checks: unexpected types")
            try:
                amounts = [Decimal(v) for v in values]
            except InvalidOperation:
                raise ValueError("text checks: unexpected amount") from None
            if not all(a.is_finite() for a in amounts):
                raise ValueError("text checks: unexpected amount")
            out[key] = values
        return out

    @classmethod
    def of(cls, text):
        return text if isinstance(text, cls) else cls(text_checks(text))


def _printed_amount(text, amount):
    """A numeric token of the text equals the amount by value (commas stripped; sign and brackets ignored)."""
    return amount in TextChecks.of(text).numbers


def printed_discount(text, amount):
    """The amount is printed on a text line with the whole word 'discount' (shared with fine_rules RF-2)."""
    return amount in TextChecks.of(text).discounts


def after_discount(text, pairs, net):
    """RF-3, the match agreed with RULES: at P = the Header net's decimals as read (at least 2), the lines total
    T = sum(qty x unit cost) is printed, and T - net > 0 is printed on a 'discount' line."""
    places = max(_places(net) or 0, 2)
    total = _q(sum((q * c for q, c in pairs), Decimal(0)), places)
    discount = total - _q(net, places)
    return discount > 0 and printed_discount(text, discount) and _printed_amount(text, total)


def missing_lines(read, net, exact, text):
    """Item lines missing or merged when lines_to_net fails (MEASURE, T2(b)): printed - read when the invoice
    prints a line count (exact evidence, uncapped), else |residual| / mean read-line net, at most 2 x the read
    lines; at least 1 either way."""
    printed_count = TextChecks.of(text).line_count
    if printed_count is not None:
        return max(printed_count - read, 1)
    if exact <= 0:
        return 1
    k = int((abs(net - exact) * read / exact).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return min(max(k, 1), 2 * read)


def arithmetic(view, sources):
    """Line totals vs Header net, net + tax = printed gross, Tax_Breakdown vs Header, at the currency's decimals."""
    currency = ((view.get("fields") or {}).get("currency") or {}).get("value")
    places = decimals(currency)
    net, tax = _value(view, "net"), _value(view, "tax")
    out = []

    lines = view.get("lines") or []
    pairs = [(_line_value(l, "Quantity"), _line_value(l, "Unit Cost")) for l in lines]
    if net is None or not lines or any(q is None or c is None for q, c in pairs):
        out.append({"check": "lines_to_net", "status": SKIPPED,
                    "detail": "Header net or a line quantity / unit cost is empty"})
    else:
        exact = _q(sum((q * c for q, c in pairs), Decimal(0)), places)
        per_line = sum((_q(q * c, places) for q, c in pairs), Decimal(0))
        ok = _q(net, places) in (exact, per_line)
        # Unit costs printed rounded (line totals from the unrounded price): each line may be off by up to
        # quantity x half a unit-cost step. Within that bound it is a warning, never a pass.
        step = max(Decimal(1).scaleb(c.as_tuple().exponent) for _, c in pairs)
        bound = sum((abs(q) for q, _ in pairs), Decimal(0)) * step / 2 + Decimal(1).scaleb(-places)
        rounded = not ok and abs(exact - _q(net, places)) <= bound
        # An invoice-level discount printed in the totals: the lines carry the pre-discount prices (RF-3).
        discounted = not ok and not rounded and after_discount(sources.text, pairs, net)
        status, detail = ((PASS, "Sum of quantity x unit cost equals the Header net") if ok else
                          (WARNING, "Sum of quantity x unit cost differs from the Header net within unit-cost "
                                    "rounding") if rounded else
                          (WARNING, "Lines equal the Header net after the printed invoice discount") if discounted else
                          (FAIL, "Sum of quantity x unit cost differs from the Header net"))
        check = {"check": "lines_to_net", "status": status, "decimals": places, "detail": detail}
        if status == FAIL and sources.text.unavailable:
            check.update(unavailable=True, detail=detail + "; the printed discount and line count could not be checked")
        if status == FAIL:
            check["missing_lines"] = k = missing_lines(len(lines), _q(net, places), exact, sources.text)
            check["detail"] += f"; {k} item line(s) missing or merged"
        out.append(check)

    if net is None or tax is None:
        out.append({"check": "net_plus_tax_gross", "status": SKIPPED, "detail": "Header net or tax is empty"})
    else:
        gross = _q(net + tax, places)
        found = printed(sources.text, f"{gross:f}")
        if found is None:
            status, detail = SKIPPED, (UNAVAILABLE_DETAIL if sources.text.unavailable else
                                       "No text layer to find the printed gross")
        elif found:
            status, detail = PASS, "Net + tax is printed on the invoice as the gross"
        else:
            # No gross column in the target sheet: an unprinted gross is a warning, not a failure.
            status, detail = WARNING, "Net + tax is not printed on the invoice"
        out.append({"check": "net_plus_tax_gross", "status": status, "decimals": places, "detail": detail,
                    **({"unavailable": True} if sources.text.unavailable else {})})

    code = ((view.get("fields") or {}).get("taxCode") or {}).get("value")
    if net is None or tax is None or _blank(code):
        out.append({"check": "tax_breakdown_to_header", "status": SKIPPED,
                    "detail": "Tax code, Header net or tax is empty"})
    else:
        rate = _vat_rate(sources.config, code)
        if rate is None:
            out.append({"check": "tax_breakdown_to_header", "status": SKIPPED,
                        "detail": "No owner VAT rate for the tax code; basis equals Header net by construction"})
        else:
            # Invoices round VAT on the total or per line and sum it; either rounding is the owner's arithmetic.
            allowed = {_q(net * rate / 100, places)}
            if lines and all(q is not None and c is not None for q, c in pairs):
                allowed.add(sum((_q(_q(q * c, places) * rate / 100, places) for q, c in pairs), Decimal(0)))
            ok = _q(tax, places) in allowed
            out.append({"check": "tax_breakdown_to_header", "status": PASS if ok else FAIL, "decimals": places,
                        "detail": "Tax basis x code rate (total or per line) equals the Header tax" if ok else
                        "Tax basis x code rate differs from the Header tax"})
    return out


def planned_rows(view, transaction=1, upc="empty"):
    """The rows the workbook writes for this invoice, typed the way excel.rules_workbook types them."""
    f = view.get("fields") or {}

    def v(key):
        value = (f.get(key) or {}).get("value")
        return None if _blank(value) else value

    def ident(value):
        return int(value) if value is not None and re.fullmatch(r"[1-9]\d{0,14}", str(value)) else value

    def num(value):
        return _dec(value) if value is not None else None
    day = v("date")
    try:
        day = date.fromisoformat(day) if day else None
    except ValueError:
        pass
    header = [transaction, v("number"), ident(v("site")), ident(v("po")), ident(v("location")), v("location_type"),
              day, num(v("net")), num(v("tax")), None, None, None, None]
    tax = [transaction, v("taxCode"), num(v("net"))]
    details = []
    for line in view.get("lines") or []:
        c = line.get("cells") or {}
        cv = lambda k: None if _blank((c.get(k) or {}).get("value")) else c[k]["value"]  # noqa: E731
        details.append([transaction, ident(cv("Item")), cv("UPC") if upc == "barcode" else None,
                        num(cv("Unit Cost")), num(cv("Quantity")), cv("Unit Tax Code")])
    return {"Header": [TEMPLATE["Header"], header], "Tax_Breakdown": [TEMPLATE["Tax_Breakdown"], tax],
            "Details": [TEMPLATE["Details"], *details]}


def joins(sheets):
    """Every Details and Tax_Breakdown row joins to exactly one Header row; no orphans and no duplicates."""
    def keys(name):
        return [row[0] for row in sheets.get(name, [])[1:] if row and any(x not in (None, "") for x in row)]
    header, tax, details = keys("Header"), keys("Tax_Breakdown"), keys("Details")
    if not details:
        # The read produced no item lines, so there are no Details rows to join to the Header. That
        # gap is already reported by the empty cells and by the skipped lines_to_net check: calling
        # it a broken target sheet names the wrong cause on every such invoice.
        return [{"check": "joins", "status": SKIPPED,
                 "detail": "No item lines were read: there are no Details rows to join"}]
    counts = Counter(header)
    problems = []
    duplicates = sorted(str(k) for k, n in counts.items() if n > 1)
    if duplicates:
        problems.append(f"{len(duplicates)} transaction number(s) repeat in Header")
    tax_counts = Counter(tax)
    if any(n > 1 for n in tax_counts.values()):
        problems.append("A transaction has more than one Tax_Breakdown row")
    orphans = sum(1 for k in tax + details if counts.get(k) != 1)
    if orphans:
        problems.append(f"{orphans} Tax_Breakdown/Details row(s) do not join to exactly one Header")
    childless = sum(1 for k in counts if k not in tax_counts or k not in set(details))
    if childless:
        problems.append(f"{childless} Header row(s) without Tax_Breakdown or Details rows")
    return [{"check": "joins", "status": FAIL if problems else PASS,
             "detail": "; ".join(problems) or "Every row joins to exactly one Header"}]


def _places(value):
    d = _dec(value)
    return max(0, -d.as_tuple().exponent) if d is not None and d.is_finite() else None


def template(sheets, currency_places=None):
    """Column order and count, cell types and amount decimals against the owner's template."""
    problems = []
    for name, columns in TEMPLATE.items():
        rows = sheets.get(name)
        if not rows:
            problems.append(f"{name} sheet is missing")
            continue
        if list(rows[0]) != columns:
            problems.append(f"{name} columns differ from the template (order or count)")
        for row in rows[1:]:
            if len(row) != len(columns):
                problems.append(f"{name} row has {len(row)} cells, template has {len(columns)}")
                continue
            cell = dict(zip(columns, row))
            if not isinstance(cell["Transaction Number"], int) or isinstance(cell["Transaction Number"], bool):
                problems.append(f"{name} Transaction Number is not a whole number")
            for column, value in cell.items():
                if isinstance(value, str) and value.startswith(("=", "+", "@")) and len(value) > 1 \
                        and _dec(value) is None:
                    problems.append(f"{name} {column} looks like a formula")
            if name == "Header":
                for column in ("Supplier Site", "Order No", "Location"):
                    if cell[column] not in (None, "") and not re.fullmatch(r"[1-9]\d{0,14}", str(cell[column])):
                        problems.append(f"Header {column} is not an owner id")
                if cell["Location Type"] not in (None, "", *LOCATION_TYPES):
                    problems.append("Header Location Type is not Store (S) or Warehouse (W)")
                if cell["Document Date"] not in (None, "") and not isinstance(cell["Document Date"], (date, datetime)):
                    problems.append("Header Document Date is not a date")
                for column in OWNER_EMPTY:
                    if cell[column] not in (None, ""):
                        problems.append(f"Header {column} must stay empty")
                amounts = ("Total Cost Ex Tax", "Tax Amount")
            elif name == "Tax_Breakdown":
                amounts = ("Tax Basis",)
            else:
                amounts = ()
                for column in ("Unit Cost", "Quantity"):
                    if cell[column] not in (None, "") and _dec(cell[column]) is None:
                        problems.append(f"Details {column} is not a number")
                if cell["Item"] not in (None, "") and not re.fullmatch(r"[1-9]\d{0,14}", str(cell["Item"])):
                    problems.append("Details Item is not an Item Master id")
            for column in amounts:
                value = cell[column]
                if value in (None, ""):
                    continue
                if isinstance(value, str) or _dec(value) is None:
                    problems.append(f"{name} {column} is not a number")
                elif currency_places is not None and (_places(value) or 0) > currency_places:
                    problems.append(f"{name} {column} has more decimals than the currency")
    unique = list(dict.fromkeys(problems))
    return [{"check": "template", "status": FAIL if unique else PASS,
             "detail": "; ".join(unique[:8]) or "Columns, order, types and decimals match the owner's template"}]


# --------------------------------------------------------------------------- the per-invoice check


def missing_line_cells(checks, upc="empty"):
    """The Details cells of the item lines a failed lines_to_net counts as missing: never matched."""
    k = next((c.get("missing_lines", 0) for c in checks if c["check"] == "lines_to_net"), 0)
    cells = []
    for i in range(1, k + 1):
        for column in TEMPLATE["Details"][1:]:
            if column == "UPC" and upc == "empty":
                cells.append(_cell("Details", column, f"missing {i}", None, OWNER_RULE_EMPTY, reason=UPC_EMPTY_REASON))
            else:
                cells.append(_cell("Details", column, f"missing {i}", None, MISSING_LINE, reason=MISSING_LINE_REASON))
    return cells


def counts(cells, scope=None):
    picked = [c for c in cells if scope is None or c["scope"] == scope]
    by, groups = Counter(c["status"] for c in picked), Counter(c["group"] for c in picked)
    return {"cells": len(picked), **{g: groups.get(g, 0) for g in GROUPS},
            "buckets": {s: by.get(s, 0) for s in STATUSES},
            "owner_entry": sum(1 for c in picked if c["sub"] == OWNER_ENTRY)}


def check_view(view, sources, transaction=1, upc="empty", entries=None, attribution=None, now=None):
    """The whole read-only check for one invoice's target-sheet rows."""
    cells = sheet_cells(view, sources, transaction, upc, entries, attribution)
    currency = ((view.get("fields") or {}).get("currency") or {}).get("value")
    sheets = planned_rows(view, transaction, upc)
    checks = arithmetic(view, sources) + joins(sheets) + template(sheets, decimals(currency))
    cells += missing_line_cells(checks, upc)
    total, metric = counts(cells), counts(cells, "metric")
    failed = [c["check"] for c in checks if c["status"] == FAIL]
    return {
        "version": 1, "checked_at": (now or datetime.now(timezone.utc)).isoformat(), "transaction": transaction,
        "upc": upc, "counts": total, "metric": metric, "cells": cells, "checks": checks,
        # Names only, never values: the inbox is served the trimmed check and still has to say which
        # row check failed rather than that "the target check failed".
        "failed": failed,
        "holds": bool(total[NEEDS_CHECKING] or failed),
        # Printed-text cells are unverifiable and the gross check skipped while the text checks are unavailable.
        "text_checks": "unavailable" if sources.text.unavailable else "checked",
        "summary": summary_line(total, failed),
    }


def summary_line(c, failed=()):
    # Cells are scored over the rows read; missing lines cost no cells, so a partial read says so here.
    return (f"Target sheet: {c[VERIFIED]} verified · {c[OWNER_RULE_EMPTY]} empty by owner rule · "
            f"{c[EMPTY]} empty (flagged) · {c[NEEDS_CHECKING]} needs checking"
            + (" · lines do not sum to net" if "lines_to_net" in failed else ""))


def issues(result):
    """Review-level banner issues: the owner's confirm is the final say (Dispatcher decision 12)."""
    out = []
    for core, code, message in ((True, "Target Mismatch", "cell(s) do not equal the source their evidence points to"),
                                (False, "Target Unverified", "filled cell(s) could not be proven from their evidence")):
        # Missing-line cells are reported by the failed lines_to_net check below, not as unproven filled cells.
        cells = [x for x in result["cells"] if x["group"] == NEEDS_CHECKING and x["status"] != MISSING_LINE
                 and (x["status"] == MISMATCH) == core]
        if cells:
            fields = sorted({f"{x['sheet']}.{x['column']}" for x in cells})
            out.append({"code": code, "message": f"{len(cells)} {message}: {', '.join(fields)}", "owner": "Accounts payable",
                        "line": None, "rule": "TARGET-CHECK", "evidence": "", "blocking": True, "level": "review"})
    for check in result["checks"]:
        if check["status"] == FAIL:
            out.append({"code": "Target Check", "message": check["detail"], "owner": "Accounts payable", "line": None,
                        "rule": f"TARGET-{check['check'].upper()}", "evidence": "", "blocking": True, "level": "review"})
    return out


def log_fields(result):
    """What may be logged: counts and field names, never values."""
    return {"counts": result["counts"], "metric": result["metric"],
            "not_verified": sorted({f"{x['sheet']}.{x['column']}" for x in result["cells"]
                                    if x["group"] == NEEDS_CHECKING}),
            "checks": {c["check"]: c["status"] for c in result["checks"]}}


# --------------------------------------------------------------------------- workbook level


def read_workbook(content):
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(content))
    return {s.title: [list(r) for r in s.iter_rows(values_only=True)] for s in book.worksheets}


def check_workbook(content, currencies=None):
    """Joins and template over a written target workbook (all of its invoices)."""
    sheets = read_workbook(content)
    places = {decimals(c) for c in (currencies or [])}
    return joins(sheets) + template(sheets, max(places) if places else None)


CHECKS_COLUMNS = ["Transaction Number", "Sheet", "Column", "Line", "Value", "Status", "Detail", "Reason",
                  "Evidence Kind", "Evidence Source", "Evidence Reference", "Evidence Original", "Rule"]


def checks_rows(results, workbook_checks=()):
    """Rows of the workbook's 'Checks' sheet: every cell's status and evidence, then the row-level checks."""
    rows = []
    for r in results:
        t = r["transaction"]
        for c in r["cells"]:
            e = c["evidence"]
            rows.append({"Transaction Number": t, "Sheet": c["sheet"], "Column": c["column"], "Line": c["line"] or "",
                         "Value": c["value"], "Status": c["group"],
                         "Detail": c["status"] + (" (owner entry)" if c["sub"] == OWNER_ENTRY else ""), "Reason": c["reason"],
                         "Evidence Kind": e["kind"], "Evidence Source": e["source"],
                         "Evidence Reference": e["reference"], "Evidence Original": e["original"], "Rule": e["rule"]})
        for k in r["checks"]:
            rows.append({"Transaction Number": t, "Sheet": "(check)", "Column": k["check"], "Status": k["status"],
                         "Reason": k["detail"]})
    for k in workbook_checks:
        rows.append({"Transaction Number": "", "Sheet": "(workbook)", "Column": k["check"], "Status": k["status"],
                     "Reason": k["detail"]})
    return rows


def add_checks_sheet(content, results, currencies=None):
    """Append the 'Checks' sheet to a written target workbook; the three template tabs are untouched."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill
    workbook_checks = check_workbook(content, currencies)
    book = load_workbook(io.BytesIO(content))
    sheet = book.create_sheet("Checks")
    sheet.append(CHECKS_COLUMNS)
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="173D3B")
    for row in checks_rows(results, workbook_checks):
        sheet.append([None] * len(CHECKS_COLUMNS))
        for i, column in enumerate(CHECKS_COLUMNS, 1):
            value = row.get(column, "")
            cell = sheet.cell(sheet.max_row, i)
            if isinstance(value, int) and not isinstance(value, bool):
                cell.value = value
            else:
                # Text only: owner values and quotes are never written as formulas.
                cell.value = "" if value is None else str(value)
                cell.data_type = "s"
    for column, width in zip("ABCDEFGHIJKLM", (12, 14, 18, 6, 22, 14, 22, 36, 12, 30, 28, 36, 14)):
        sheet.column_dimensions[column].width = width
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue(), workbook_checks


# --------------------------------------------------------------------------- accuracy against owner truth


def _key(cell):
    return (cell["sheet"], cell["column"], cell["line"])


def confirm_records(system, final, supplier="", at=None):
    """Per target-sheet cell at the owner's confirm: field, its status before, and whether the owner changed
    the system's value. Records hold no values."""
    at = (at or datetime.now(timezone.utc)).isoformat()
    before = {_key(c): c for c in (system or {}).get("cells", []) if c["scope"] == "metric"}
    after = {_key(c): c for c in (final or {}).get("cells", []) if c["scope"] == "metric"}
    records = []
    # Read lines are numbered; missing-line cells carry 'missing i' and sort after them.
    order = lambda k: (k[0], k[1], isinstance(k[2], str), k[2] if isinstance(k[2], int) else 0, str(k[2] or ""))  # noqa: E731
    for key in sorted(set(before) | set(after), key=order):
        b, a = before.get(key), after.get(key)
        old, new = (b or {}).get("value", ""), (a or {}).get("value", "")
        changed = not (old == new or (old and new and same(new, old)))
        records.append({"at": at, "supplier": str(supplier or ""), "field": f"{key[0]}.{key[1]}", "line": key[2],
                        "status_before": (b or {}).get("status", "absent"), "changed": changed})
    return records


def _rate(n, changed):
    return {"cells": n, "unchanged": n - changed, "changed": changed,
            "accuracy": round((n - changed) / n, 4) if n else None}


def accuracy_summary(records, now=None, supplier=None):
    """Per field and overall, by status before confirm and per supplier code, for 7 / 30 days and all time."""
    now = now or datetime.now(timezone.utc)
    periods = {"7d": now - timedelta(days=7), "30d": now - timedelta(days=30), "all": None}
    out = {"generated_at": now.isoformat(), "periods": {}}
    for name, since in periods.items():
        picked = [r for r in records if (since is None or datetime.fromisoformat(r["at"]) >= since)
                  and (supplier is None or r.get("supplier") == supplier)]
        group = defaultdict(lambda: [0, 0])
        for r in picked:
            # A missing line (line 'missing i') has no value to confirm: it shows under its status only, never as accurate.
            keys = (("status", r["status_before"]),) if isinstance(r["line"], str) else \
                (("overall",), ("field", r["field"]), ("status", r["status_before"]), ("supplier", r.get("supplier") or ""))
            for k in keys:
                group[k][0] += 1
                group[k][1] += bool(r["changed"])
        invoices = len({(r.get("job_id"), r["at"]) for r in picked})
        out["periods"][name] = {
            "confirms": invoices,
            "overall": _rate(*group[("overall",)]),
            "fields": {k[1]: _rate(*v) for k, v in sorted(group.items()) if k[0] == "field"},
            "by_status_before": {k[1]: _rate(*v) for k, v in sorted(group.items()) if k[0] == "status"},
            "suppliers": {k[1]: _rate(*v) for k, v in sorted(group.items()) if k[0] == "supplier"},
        }
    return out
