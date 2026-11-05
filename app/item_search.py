"""The installed item master as rules_first's item search: ``for_invoice`` builds one invoice's
``item_search(line) -> search(column, match)``, each search answered by ``search(source, line, column, match)``.

``source`` is a fine_rules_source.LookupRulesSource over the live catalog (lookup_rows/lookup_terms). One call
answers one rules workbook row for one line: the line's ITEM_PARENT candidates in that item master column, best
first, limited to the invoice's supplier site when it has one ("supplier_site"). Barcodes match without the item
master's ULT_ prefix on either side (matching.strip_ult, R-004). Each column is read from the line fields below;
a column the rules name that is not here is refused, so a new workbook row cannot silently match nothing. With no item master installed every search is empty, and
matching.lookup_item ends on Not in item master.
"""
from decimal import Decimal, InvalidOperation

from .fine_rules import description_score
from .item_rules import RulesError
from .matching import strip_ult

# The line fields a search reads; the job child sends only these (and the supplier site) to the API process.
LINE_FIELDS = ("sku", "part_code", "gtin", "barcode_unchecked", "description", "qty")


def _values(line, *names):
    return list(dict.fromkeys(str(line[n]).strip() for n in names if str(line.get(n) or "").strip()))


def _barcodes(line):
    return list(dict.fromkeys(b for b in map(strip_ult, _values(line, "gtin", "barcode_unchecked")) if b))


def _items(line, rows):
    """Item rows at the line's supplier site, when the line names one (PO rows carry no site)."""
    site = str(line.get("supplier_site") or "").strip()
    return [r for r in rows if not site or str(r.get("SUPPLIER SITE", "")).strip() == site]


def _quantity(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return format(number.normalize(), "f") if number.is_finite() and number > 0 else None


def _fuzzy(source, line, description):
    best = {}
    for row in _items(line, source.items_by_description(description)):
        score = description_score(description, row)
        if score > 0:
            best[row["ITEM_PARENT"]] = max(score, best.get(row["ITEM_PARENT"], 0))
    return sorted(best, key=lambda parent: -best[parent])


COLUMNS = {
    ("VPN", "exact"): lambda s, line: [r["ITEM_PARENT"] for v in _values(line, "sku", "part_code")
                                       for r in _items(line, s.items_by_vpn(v))],
    ("BARCODE", "exact"): lambda s, line: [r["ITEM_PARENT"] for v in _barcodes(line)
                                           for r in _items(line, s.items_by_barcode(v))],
    ("BARCODE_1", "exact"): lambda s, line: [r["RMS_ITEM_ID"] for v in _barcodes(line)
                                             for r in s.orders_by_barcode(v)],
    ("ITEM_DESC", "fuzzy"): lambda s, line: [p for v in _values(line, "description") for p in _fuzzy(s, line, v)],
    ("QTY_RECEIVED", "number"): lambda s, line: [r["RMS_ITEM_ID"] for q in [_quantity(line.get("qty"))] if q
                                                 for r in s.orders_by_quantity(q)],
}


def invoice_sites(source, invoice):
    """The invoice's resolved supplier sites: its site code when read, else the item master's sites under its
    supplier name (SUPPLIER_NAME or SUPPLIER SITE NAME)."""
    if str(invoice.get("site") or "").strip():
        return [str(invoice["site"]).strip()]
    name = str(invoice.get("supplier_name") or "").strip()
    return sorted({str(r.get("SUPPLIER SITE", "")) for r in source.items_by_supplier_name(name)} - {""}) if name else []


def for_invoice(ask, sites, site=None):
    """``item_search(line)`` for one invoice; ``ask(line, column, match)`` runs one search.

    Item rows are limited to ``site`` when given (the reviewer's pick), else to the invoice's resolved site when
    ``sites()`` returns exactly one; none or several leave the search unlimited."""
    if site is None:
        found = sites()
        site = found[0] if len(found) == 1 else None

    def item_search(line):
        sent = {**{name: line.get(name) for name in LINE_FIELDS}, "supplier_site": site}
        return lambda column, match: ask(sent, column, match)
    return item_search


def search(source, line, column, match):
    run = COLUMNS.get((column, match))
    if run is None:
        raise RulesError(f"The item master search cannot run a {match} match on {column}")
    return list(dict.fromkeys(str(p) for p in run(source, line) if str(p or "").strip()))
