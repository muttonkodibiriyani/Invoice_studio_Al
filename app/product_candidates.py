"""Approximate product discovery. Scores are clues, never approval decisions."""
import json
import re
import threading
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz, process

from .reference_lookup import normalize_identifier, normalize_name


_NUMBER = re.compile(r"^[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\.\d+)$")
_MAX_CLUE = Decimal("1e18")


def numeric(value):
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if len(text) > 80 or not _NUMBER.fullmatch(text):
            return None
        result = Decimal(text.replace(",", ""))
        return result if result.is_finite() and abs(result) <= _MAX_CLUE else None
    except (InvalidOperation, ValueError):
        return None


def similarity(left, right):
    if left is None or right is None:
        return None
    if left == right:
        return 1.0
    try:
        return float(
            max(
                Decimal(0),
                1 - abs(left-right)/max(abs(left), abs(right), Decimal("0.000001")),
            )
        )
    except (InvalidOperation, OverflowError):
        return None


def descriptions(record):
    sources = record.get("candidate_field_sources", {}).get("description", [])
    category = {"dept name", "class name", "sub name", "category", "department"}
    values = [str(s["value"]) for s in sources if normalize_name(s.get("column", "")) not in category]
    return values or record.get("candidate_fields", {}).get("description", [])


def role_values(record, role):
    return {
        normalize_name(value)
        for value in record.get("candidate_fields", {}).get(role, [])
        if normalize_name(value)
    }


def unsafe_source_flags(source):
    """Return flags which say a source row is unsafe for numeric ranking.

    Other flags remain visible but do not automatically suppress a clue. For
    example, a repeated PO item group can be legitimate and still offers a
    price or quantity clue. Structural, shifted, or quarantined rows cannot.
    """
    unsafe_words = ("structur", "shift", "quarant")
    return [
        flag for flag in source.get("flags", [])
        if any(word in normalize_name(flag) for word in unsafe_words)
    ]


class ProductCandidates:
    def __init__(self, lookup):
        self.lookup = lookup
        self.store = lookup.store
        self._lock = threading.Lock()
        self._version = None
        self._names = []

    def names(self):
        # Cache only normalized names, not 412,000 business payloads. Refresh
        # after a complete catalog changes; never cache approvals or balances.
        with self.store.connection() as c:
            version = tuple(row[0] for row in c.execute("SELECT id FROM lookup_sources ORDER BY id"))
        if version == self._version:
            return self._names
        with self._lock:
            if version != self._version:
                with self.store.connection() as c:
                    # Locale-aware PostgreSQL collations can order ``n;``
                    # before ``n:word``. LIKE is portable and keeps the prefix
                    # boundary exact on both PostgreSQL and SQLite.
                    rows = c.execute(
                        """SELECT DISTINCT term FROM lookup_terms
                           WHERE kind='item' AND term LIKE ?
                           ORDER BY term LIMIT 250001""",
                        ("n:%",),
                    ).fetchall()
                if len(rows)>250000:
                    raise ValueError("Product name catalog exceeds this workspace's search limit")
                self._names = [row[0][2:] for row in rows]
                self._version = version
        return self._names

    def _po_context(self, records, po):
        if not po or not records:
            return {}
        marks=','.join('?' for _ in records)
        with self.store.connection() as c:
            rows=c.execute(
                f"""SELECT DISTINCT ik.row_id AS item_row_id,r.id,r.payload FROM lookup_terms ik
                   JOIN lookup_terms pk ON pk.kind='po' AND pk.term=ik.term
                   JOIN lookup_terms pf ON pf.kind='po' AND pf.row_id=pk.row_id AND pf.term=?
                   JOIN lookup_rows r ON r.id=pk.row_id
                   WHERE ik.kind='item' AND ik.row_id IN ({marks}) AND ik.term LIKE ?
                   ORDER BY ik.row_id,r.id LIMIT 1001""",
                ("f:po:"+normalize_identifier(po),*[r['id'] for r in records],"x:%")
            ).fetchall()
        result={}
        for row in rows:
            bucket=result.setdefault(row['item_row_id'],[])
            if len(bucket)<21:bucket.append(json.loads(row['payload']))
        return result

    def search(self, query, *, limit=25, site="", supplier="", po="", invoice_po="", price="", qty="", currency="", uom=""):
        q = normalize_name(query)
        if not 2<=len(q)<=100 or not 1<=limit<=50:
            raise ValueError("Enter 2 to 100 characters and a result limit from 1 to 50")
        if any(len(str(v))>300 for v in (site,supplier,po,invoice_po,price,qty,currency,uom)):
            raise ValueError("Search context is too long")
        wanted_price,wanted_qty=numeric(price),numeric(qty)
        if price and wanted_price is None or qty and wanted_qty is None:
            raise ValueError("Price and quantity clues must be finite numbers")
        names=self.names()
        filters={"site":normalize_name(site),"supplier":normalize_name(supplier),"po":normalize_identifier(po)}
        # WRatio tolerates abbreviations/reordered words. Rerank the nearest
        # names with a whole-string component so a broad department name does
        # not crowd every more specific product out of the candidate pool.
        nearest=process.extract(q,names,scorer=fuzz.WRatio,limit=200,score_cutoff=55)
        nearest.sort(key=lambda x:-(.6*x[1]+.4*fuzz.ratio(q,x[0])))
        weighted_terms=[("i:"+normalize_identifier(query),1_000_000)]
        weighted_terms.extend(
            ("n:"+choice,int(round(1000*(.6*score+.4*fuzz.ratio(q,choice)))))
            for choice,score,_ in nearest[:40]
        )
        wanted=",".join("(?,?)" for _ in weighted_terms)
        clauses=[];params=[value for pair in weighted_terms for value in pair]
        for role,value in filters.items():
            if not value:
                continue
            if role=="po":
                clauses.append("""EXISTS(SELECT 1 FROM lookup_terms ik
                    JOIN lookup_terms pk ON pk.kind='po' AND pk.term=ik.term
                    JOIN lookup_terms pf ON pf.kind='po' AND pf.row_id=pk.row_id AND pf.term=?
                    WHERE ik.kind='item' AND ik.row_id=t.row_id AND ik.term LIKE ?)""")
                params.extend(("f:po:"+value,"x:%"))
            else:
                clauses.append("EXISTS(SELECT 1 FROM lookup_terms f WHERE f.kind='item' AND f.row_id=t.row_id AND f.term=?)")
                params.append(f"f:{role}:{value}")
        constraints=(" AND "+" AND ".join(clauses)) if clauses else ""
        sql=f"""WITH wanted(term,weight) AS (VALUES {wanted})
                SELECT r.payload FROM (SELECT t.row_id AS id,MAX(w.weight) AS candidate_weight
                FROM lookup_terms t JOIN wanted w ON w.term=t.term
                WHERE t.kind='item' {constraints}
                GROUP BY t.row_id ORDER BY candidate_weight DESC,t.row_id LIMIT 501) picked
                JOIN lookup_rows r ON r.id=picked.id"""
        with self.store.connection() as c:
            rows=c.execute(sql,tuple(params)).fetchall()
        records=[]
        for row in rows[:500]:
            record=json.loads(row["payload"]);fields=record["candidate_fields"]
            exact=normalize_identifier(query) in {normalize_identifier(v) for role in ("sku","gtin","internal_item") for v in fields.get(role,[])}
            name_score=max((fuzz.WRatio(q,normalize_name(v))/100 for v in descriptions(record)),default=0)
            if not exact and name_score<.55:
                continue
            record=self.lookup._public_candidate(record,q,0,filters)
            record["match"].update(score=1.0 if exact else round(.85*name_score,3),name_similarity=round(name_score,3),
                basis=["exact_identifier" if exact else "approximate_product_name"],clues=[])
            records.append(record)
        records.sort(key=lambda r:(-r["match"]["score"],r["id"]))
        # Enrich a bounded candidate pool with source-proven PO rows only.
        # Keep identity/price/quantity separate and never alter invoice values.
        context_po=po or invoice_po
        context_map=self._po_context(records[:50],context_po) if context_po and (wanted_price is not None or wanted_qty is not None) else {}
        for record in records[:50]:
            contexts=context_map.get(record["id"],[])
            clues=[];price_scores=[];qty_scores=[]
            same_uom=bool(uom) and normalize_name(uom) in {normalize_name(v) for v in record["candidate_fields"].get("uom",[])}
            for source in contexts[:20]:
                data=source["data"];ref_price=numeric(data.get("UNIT_COST"));ref_currency=str(data.get("CURRENCY_CODE") or "").strip().upper()
                role_conflicts=[]
                for role in ("site","supplier"):
                    item_values=role_values(record,role);po_values=role_values(source,role)
                    if item_values and po_values and item_values.isdisjoint(po_values):
                        role_conflicts.append(role)
                unsafe_flags=unsafe_source_flags(source)
                scoring_eligible=not unsafe_flags and not role_conflicts
                clue={"source_row_id":source["id"],"source_sheet":source["source_sheet"],"source_row":source["source_row"],
                    "flags":source["flags"],"po":data.get("RMS_ORDER_NO"),"unit_price":data.get("UNIT_COST"),"currency":ref_currency,
                    "ordered_qty":data.get("QTY_ORDERED"),"received_qty":data.get("QTY_RECEIVED"),"quantity_units_confirmed":False,
                    "scoring_eligible":scoring_eligible}
                if unsafe_flags:
                    clue["not_scored_reason"]="PO source row is flagged as structurally unsafe, shifted or quarantined"
                elif role_conflicts:
                    clue["not_scored_reason"]="Known item and PO source identifiers conflict for: "+", ".join(role_conflicts)
                if not scoring_eligible:
                    if wanted_price is not None:clue["price_note"]="Price not scored: "+clue["not_scored_reason"]
                    if wanted_qty is not None:clue["quantity_note"]="Quantity not scored: "+clue["not_scored_reason"]
                elif wanted_price is not None and ref_price is not None and currency and ref_currency==currency.strip().upper() and same_uom:
                    clue["price_similarity"]=similarity(wanted_price,ref_price);price_scores.append(clue["price_similarity"])
                    clue["price_note"]="Same currency and item standard UOM; verify PO pack/unit basis before accepting"
                elif wanted_price is not None:
                    clue["price_note"]="Price not scored: currency or item UOM is missing/different; no FX or pack conversion"
                quantity_scores=[] if not scoring_eligible else [similarity(wanted_qty,numeric(data.get(k))) for k in ("QTY_ORDERED","QTY_RECEIVED")]
                quantity_scores=[v for v in quantity_scores if v is not None]
                if quantity_scores:
                    clue["quantity_similarity"]=max(quantity_scores);qty_scores.append(clue["quantity_similarity"])
                    clue["quantity_note"]="Raw quantity clue only; partial receipts and unit/pack differences require confirmation"
                clues.append(clue)
            match=record["match"]
            match["clues"]=clues
            if "exact_identifier" not in match["basis"]:
                match["score"]=round(min(.99,match["score"]+.10*max(price_scores,default=0)+.05*max(qty_scores,default=0)),3)
            if price_scores:match["basis"].append("po_price_similarity")
            if qty_scores:match["basis"].append("po_quantity_similarity")
            if len(contexts)>20:match["clues_truncated"]=True
        records.sort(key=lambda r:(-r["match"]["score"],r["id"]))
        notice="Suggested products need your confirmation. Similarity is a ranking clue, not extraction accuracy or approval. Invoice prices and quantities are unchanged."
        if not names:notice="The Item Master lookup catalog is not loaded yet. Product search needs that import to finish."
        if len(rows)>500:notice+=" Candidate pool limited to 500 rows; refine the name, supplier, site or PO."
        return {"kind":"item","query":query,"records":records[:limit],"filters":filters,"next_cursor":None,
                "requires_confirmation":True,"approved_for_matching":False,"notice":notice}
