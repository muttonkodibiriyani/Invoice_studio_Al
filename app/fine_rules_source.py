"""Read-only adapter from the imported lookup catalog to the fine-grained rules engine.

The catalog keeps every original column in ``payload.data``. Terms are only an
index; the engine re-checks every exact value against the original column, so
a normalized term can widen the search but never decide a match.
"""

import json

from app.fine_rules import normalize_description
from app.reference_lookup import normalize_identifier, normalize_name


MAX_ROWS = 50_000
MAX_DESCRIPTION_ROWS = 500


class LookupRulesSource:
    def __init__(self, store, max_rows=MAX_ROWS):
        self.store = store
        self.max_rows = max_rows
        self._cache = {}

    def _rows(self, kind, terms, limit=None, site_terms=()):
        terms = sorted({t for t in terms if t})
        if not terms:
            return []
        key = (kind, tuple(terms), limit, tuple(sorted(site_terms)))
        if key in self._cache:
            return self._cache[key]
        cap = limit or self.max_rows
        marks = ",".join("?" for _ in terms)
        sql = ("SELECT DISTINCT r.id, r.sheet, r.row_number, r.payload FROM lookup_terms t "
               "JOIN lookup_rows r ON r.id=t.row_id "
               f"WHERE t.kind=? AND t.term IN ({marks})")
        params = [kind, *terms]
        if site_terms:
            site_marks = ",".join("?" for _ in site_terms)
            sql += (" AND EXISTS (SELECT 1 FROM lookup_terms s WHERE s.kind=t.kind AND s.row_id=r.id "
                    f"AND s.term IN ({site_marks}))")
            params.extend(site_terms)
        sql += " ORDER BY r.sheet, r.row_number LIMIT ?"
        params.append(cap + 1)
        with self.store.connection() as connection:
            found = connection.execute(sql, params).fetchall()
        if len(found) > cap:
            if limit:
                found = found[:cap]
            else:
                raise ValueError(f"Reference search returned more than {cap} {kind} rows; narrow the search")
        rows = []
        for row in found:
            payload = json.loads(row["payload"])
            data = dict(payload.get("data") or {})
            if any("quarant" in f.lower() for f in payload.get("flags", [])):
                continue
            data["SUPPLIER"] = data.get("SUPPLIER SITE", data.get("SUPPLIER"))
            data["_ref"] = f"{payload['source_hash']}:{kind}:{row['sheet']}!{row['row_number']}"
            rows.append(data)
        self._cache[key] = rows
        return rows

    def items_by_barcode(self, value):
        value = normalize_identifier(value)
        return self._rows("item", [f"x:gtin:ult{value}", f"x:gtin:{value}"]) if value else []

    def items_by_vpn(self, value):
        value = normalize_identifier(value)
        return self._rows("item", [f"x:sku:{value}"]) if value else []

    def items_by_parent(self, value):
        value = normalize_identifier(value)
        return self._rows("item", [f"x:internal_item:{value}"]) if value else []

    def items_by_supplier_name(self, value):
        value = normalize_name(value)
        return self._rows("item", [f"f:supplier:{value}"]) if value else []

    def supplier_names(self):
        """Every indexed Item Master supplier name (SUPPLIER_NAME, SUPPLIER SITE NAME), normalized as indexed."""
        if "supplier_names" not in self._cache:
            with self.store.connection() as connection:
                # A range on the (kind, term) key, not LIKE, so the index serves it.
                found = connection.execute("SELECT DISTINCT term FROM lookup_terms WHERE kind='item' "
                                           "AND term >= ? AND term < ?", ["f:supplier:", "f:supplier;"]).fetchall()
            self._cache["supplier_names"] = sorted(row["term"][len("f:supplier:"):] for row in found)
        return self._cache["supplier_names"]

    def items_by_site(self, value, limit=None):
        # Semantic uniqueness requires every scoped row, never an arbitrary first-page sample.
        value = normalize_name(value)
        return self._rows("item", [f"f:site:{value}"], limit) if value else []

    def items_by_description(self, value, sites=None):
        tokens = [t for t in normalize_name(normalize_description(value)).split() if 3 <= len(t) <= 100][:30]
        site_terms = [f"f:site:{normalize_name(s)}" for s in sites or ()]
        return self._rows("item", [f"t:{t}" for t in tokens], MAX_DESCRIPTION_ROWS, site_terms)

    def pogrn_by_ebs(self, value):
        # Manifests map EBS_SUPPLIER_CODE to the site role, the supplier role or both.
        value = normalize_name(value)
        return self._rows("po", [f"f:site:{value}", f"f:supplier:{value}"]) if value else []

    def pogrn_by_order(self, value):
        value = normalize_identifier(value)
        return self._rows("po", [f"f:po:{value}"]) if value else []

    def orders_by_barcode(self, value):
        value = normalize_identifier(value)
        return self._rows("po", [f"x:gtin:ult{value}", f"x:gtin:{value}"]) if value else []

    def orders_by_quantity(self, value, limit=MAX_DESCRIPTION_ROWS):
        """PO rows whose QTY_RECEIVED is ``value`` (a plain decimal string). Quantity has no term: the payload's
        JSON text narrows the rows and the column decides."""
        with self.store.connection() as connection:
            found = connection.execute(
                "SELECT payload FROM lookup_rows WHERE kind='po' AND payload LIKE ? ORDER BY sheet, row_number LIMIT ?",
                (f'%"QTY_RECEIVED":"{value}"%', limit)).fetchall()
        rows = [json.loads(row["payload"]) for row in found]
        return [dict(r["data"]) for r in rows if r["data"].get("QTY_RECEIVED") == value
                and not any("quarant" in f.lower() for f in r.get("flags", []))]

    def master_catalog(self):
        with self.store.connection() as c:
            sources = [json.loads(r[0]) for r in c.execute("SELECT payload FROM lookup_sources ORDER BY id")]
        return next((s["master_catalog"] for s in sources if s.get("master_catalog")), None)

    def master_orders(self, parents, sites):
        rows = self._rows("po", [f"x:internal_item:{normalize_identifier(p)}" for p in parents])
        accepted = []
        for row in rows:
            # Only the PO barcode and parent are joined. Adjacent worksheet supplier/location cells are independent.
            item_rows = self.items_by_barcode(row.get("BARCODE_1", ""))
            bridge = [r for r in item_rows if str(r.get("SUPPLIER")) in sites and
                      str(r.get("ITEM_PARENT")) == str(row.get("RMS_ITEM_ID")) and
                      str(r.get("BARCODE")) == str(row.get("BARCODE_1"))]
            if bridge:
                accepted.append({**row, "_bridge_refs": [r["_ref"] for r in bridge]})
        return accepted
