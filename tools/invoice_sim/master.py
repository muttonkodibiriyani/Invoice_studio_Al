"""The corpus's item master and rules config, in the app's own upload format. Every row is made up."""
from __future__ import annotations

import io
import zlib

import openpyxl

from app.master_catalog import HEADERS

from .generator import CURRENCIES, Corpus

LOCATION = "7100"
VAT_CODES = [("ARE", "SIMV5", "5"), ("SAU", "SIMV15", "15"), ("OMN", "SIMV10", "10"), ("GBR", "SIMV20", "20"), ("MEX", "SIMV16", "16")]


def parent_id(item_id: str) -> str:
    """The master's ITEM_PARENT for a generator item id: digits, as the real master holds them."""
    return "9" + item_id.removeprefix("SIM").zfill(8)


def site_of(supplier: str, currency: str | None) -> dict:
    digest = zlib.crc32(f"{supplier}|{currency}".encode())
    code = f"S{digest % 90000 + 10000}"
    return {"supplier_site": str(digest % 90000000 + 10000000), "currency": currency or "AED", "status": "Active",
            "supplier_name": supplier, "supplier_code": code, "site_name": f"{code}RA4{currency or 'AED'}"}


def sites(corpus: Corpus) -> dict[tuple[str, str], dict]:
    out = {}
    for inv in corpus.invoices:
        code = inv.style["totals_values"]["code"]
        out[(inv.truth["supplier_name"], code)] = site_of(inv.truth["supplier_name"], code)
    for row in corpus.master:
        out.setdefault((row["supplier"], "AED"), site_of(row["supplier"], "AED"))
    return out


def rules_config(corpus: Corpus) -> dict:
    return {"version": f"sim-{corpus.seed}", "location_master": {LOCATION: {"type": "Store (S)", "market": "ARE", "country": "ARE"}},
            "supplier_sites": list(sites(corpus).values()),
            "vat_codes": [{"region": r, "code": c, "rate": v, "active_from": "2020-01-01"} for r, c, v in VAT_CODES]}


def master_workbook(corpus: Corpus) -> bytes:
    orders, site_for = {}, {}
    for inv in corpus.invoices:
        code = inv.style["totals_values"]["code"]
        for line in inv.truth["lines"]:
            for item in [line["item_id"], *line["item_wrong_if_picked"]]:
                if item:
                    orders.setdefault(item, (inv.truth["po"], line["qty"]))
                    site_for.setdefault(item, (inv.truth["supplier_name"], code))
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Synthetic master"
    sheet.append(list(HEADERS))
    for n, row in enumerate(corpus.master, 1):
        supplier, code = site_for.get(row["item_id"], (row["supplier"], "AED"))
        if row["supplier"] != supplier:  # A rival's row: its own supplier, at its own site.
            supplier, code = row["supplier"], "AED"
        site = site_of(supplier, code)
        po, qty = orders.get(row["item_id"], ("", ""))
        parent = parent_id(row["item_id"])
        sheet.append([n, row["barcode"], parent, row["description"], row["supplier_code"] or None, row["category"], site["site_name"],
                      supplier, site["supplier_site"], LOCATION, "Sim Store", "Sim Store", None, po or None, row["barcode"], parent, qty or None])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


__all__ = ["CURRENCIES", "LOCATION", "master_workbook", "parent_id", "rules_config", "site_of", "sites"]
