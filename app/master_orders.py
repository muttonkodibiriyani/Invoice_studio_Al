"""Evidenced joins between the master's independent blocks; no receipt balances are inferred."""
import re
from collections import defaultdict

from .fine_rules import (STORE, WAREHOUSE, _refs, ebs_key, evaluate_pogrn, location_market,
                         location_type, name_key, scan_pages, text, to_decimal)


def destination(invoice, scan, directory, config):
    """Match only delivery evidence to the master directory, then retain its original coordinates."""
    candidates = []
    printed = text(invoice.location)
    delivery = []
    start_label = re.compile(r"(?i)\b(?:deliver(?:y)?\s*(?:to|address)|ship\s*to)\b\s*:?")
    boundary = re.compile(r"(?i)^\s*(?:bill(?:ing)?\s*to|sold\s*to|supplier|vendor|invoice|tax\s*invoice|tel(?:ephone)?|phone|email|fax|contact|account|vat|trn|date|p\.?o\.?|purchase\s*order|item|description|qty|quantity|unit|subtotal|total)\b")
    inline_boundary = re.compile(r"(?i)\b(?:bill(?:ing)?\s*to|sold\s*to|supplier\s*(?:tel|phone|email)|invoice\s*(?:no|number)|tel(?:ephone)?|phone|email|fax|vat|trn)\s*:")
    for page in scan["pages"].values():
        lines = page.splitlines()
        for index, line in enumerate(lines):
            label = start_label.search(line)
            if label:
                block = [inline_boundary.split(line[label.end():], maxsplit=1)[0]]
                for following in lines[index + 1:index + 4]:
                    if boundary.search(following):
                        break
                    block.append(inline_boundary.split(following, maxsplit=1)[0])
                delivery.append(" ".join(block))
    for row in directory:
        data = row["data"]
        values = [text(data.get(c)) for c in ("LOCATION NUMBER", "LOCATION NAME", "LOCATION NAME 2")]
        number = values[0]
        exact_field = printed and any(name_key(printed) == name_key(v) for v in values if v)
        has_field_evidence = exact_field and any(re.search(r"(?<!\w)" + re.escape(printed) + r"(?!\w)", block, re.I)
                                                for block in delivery)
        in_delivery = any(any(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", block, re.I)
                                  for v in values if v) for block in delivery)
        if has_field_evidence or in_delivery:
            candidates.append(row)
    by_id = defaultdict(list)
    for row in candidates:
        by_id[text(row["data"].get("LOCATION NUMBER"))].append(row)
    if len(by_id) != 1:
        return None
    number, rows = next(iter(by_id.items()))
    names = " ".join(str(v) for row in rows for v in row["data"].values())
    warehouse = bool(re.search(r"(?i)\b(?:warehouse|w/h|wh)\b", names))
    configured = location_type(number, config)
    if configured["conflict"] or warehouse and configured["type"] == STORE:
        return None
    kind = WAREHOUSE if warehouse else configured["type"] or STORE
    kind_source = "Master location directory" if warehouse else configured["source"] or "Owner Store default (M4)"
    return {"number": number, "rows": rows, "kind": kind, "kind_source": kind_source,
            "refs": ", ".join(f"{r['source_hash']}:location:{r['source_sheet']}!{r['source_row']}" for r in rows)}


def resolve(run, source, supplier_site, master_name, parents, invoiced):
    catalog = source.master_catalog()
    if not catalog:
        return None
    outcome = {"order": None, "source": None, "group": None, "validation": [], "status": "Missing PO",
               "candidates": 0, "malformed": 0,
               "receipt_controls": {"known": False, "capacities": [], "demand": {}}}
    if not supplier_site or not parents:
        run.exception("Missing PO", "Supplier site and matched items are required for the master order bridge", "M4")
        return outcome
    rows = source.master_orders(parents, {supplier_site})
    groups = defaultdict(list)
    for row in rows:
        groups[text(row.get("RMS_ORDER_NO"))].append(row)
    groups = {order: group for order, group in groups.items() if order and
              parents <= {text(r.get("RMS_ITEM_ID")) for r in group}}
    printed = text(run.invoice.po)
    printed_match = printed in groups
    if printed_match:
        groups = {printed: groups[printed]}
    elif printed:
        manual = {order: group for order, group in groups.items()
                  if any(text(r.get("MANUAL ORDER NO")) == printed for r in group)}
        if manual:
            groups = manual
        else:
            # Unrecognized printed PO may use the established scoped item/quantity fallback.
            groups = {order: group for order, group in groups.items() if quantities_agree(group, invoiced)}
    else:
        groups = {order: group for order, group in groups.items() if quantities_agree(group, invoiced)}
    outcome["candidates"] = len(groups)
    if len(groups) != 1:
        run.exception("Ambiguous PO" if groups else "Missing PO",
                      "No unique supplier-scoped RMS order agrees with the invoice evidence", "M4", owner="Buyer")
        return outcome
    order, selected = next(iter(groups.items()))
    # Every order row has an exact barcode/parent bridge to this supplier's item rows.
    order_refs = _refs(selected)
    run.trace("Order No", order, "M4", "Master PO block RMS_ORDER_NO", reference=order_refs,
              original=printed if printed_match else None)
    outcome.update(order=order, source="Master order bridge", cited=selected)
    scan = scan_pages(getattr(run, "master_text", ""), getattr(run, "master_boxes", ()))
    dest = destination(run.invoice, scan, catalog.get("locations", []), run.config)
    richer_rows = [r for r in source.pogrn_by_order(order) if "BARCODE_1" not in r and
                   text(r.get("EBS_SUPPLIER_CODE")).upper() == ebs_key(master_name) and
                   text(r.get("RMS_ORDER_NO")) == order]
    rich_evaluated, _ = evaluate_pogrn(run, richer_rows, {ebs_key(master_name)}, None, run.invoice.net,
                                      text(run.invoice.currency), supplier_site, run.invoice.supplier_name, master_name,
                                      supplier_site, parents) if richer_rows else ([], [])
    rich_candidates = [g for g in rich_evaluated if g["Items Check"] == "Pass" and
                       g["Supplier Check"] == "Pass" and (not dest or g["POGRN Location ID"] == dest["number"])]
    # An authoritative configured PO can corroborate the destination against the separate master directory.
    if not dest and len(rich_candidates) == 1:
        number = rich_candidates[0]["POGRN Location ID"]
        location_rows = [r for r in catalog.get("locations", []) if text(r["data"].get("LOCATION NUMBER")) == number]
        if location_rows:
            temp_invoice = type("Destination", (), {"location": number})()
            dest = destination(temp_invoice, {"pages": {1: "Deliver To: " + number}}, location_rows, run.config)
    if not dest:
        run.exception("Location ID", "Order resolved; no unique compatible master directory destination is evidenced",
                      "M4", owner="Buyer")
        outcome["status"] = "Location ambiguous"
        return outcome
    invoice_qty = sum(invoiced.values()) if invoiced and all(v is not None for v in invoiced.values()) else None
    rich = [g for g in rich_candidates if g["POGRN Location ID"] == dest["number"]]
    if len(rich) == 1:
        group = rich[0]
        # The existing governed PO/GRN checks remain available; unknown quantities never become availability.
        receipt_controls = receipt_capacities(group["_rows"], supplier_site, order, dest["number"], invoiced)
        run.trace("Receipt Association", order, "M4-RECEIPT", "Configured PO/GRN corroboration",
                  reference=_refs(group["_rows"]))
        if not group["_checks"]["value"]:
            run.exception("Value Review", "Configured PO/GRN value needs verification under the approved policy",
                          "M4-VALUE", owner="Buyer")
    else:
        # Derived check projection only. Original source cells and their coordinates remain unmodified.
        projection = [{**r, "EBS_SUPPLIER_CODE": ebs_key(master_name), "LOCATION": dest["number"],
                       "QTY_RECEIVED": None} for r in selected]
        evaluated, _ = evaluate_pogrn(run, projection, {ebs_key(master_name)}, invoice_qty, run.invoice.net,
                                     text(run.invoice.currency), supplier_site, run.invoice.supplier_name, master_name,
                                     supplier_site, parents)
        group = evaluated[0]
        group["_rows"] = selected
        receipt_controls = {"known": False, "capacities": [], "demand": {p: str(q) if q is not None else None for p, q in invoiced.items()}}
        run.exception("Value Review", "Master contains no order cost; verify invoice amounts under the approved policy",
                      "M4-VALUE", owner="Buyer")
    market, _ = location_market(dest["number"], run.config)
    group.update({"POGRN Row Reference": order_refs, "PO Source": "Master order bridge", "Derived PO Number": order,
                  "_master_destination": dest,
                  "_location_type": {"type": dest["kind"], "source": dest["kind_source"], "conflict": False},
                  "_market": market, "Location Type": dest["kind"], "_receipt_identity_unknown": not receipt_controls["known"]})
    group["_checks"]["type"] = True
    if not receipt_controls["known"]:
        run.exception("Receipt Evidence", "No authoritative unique receipt identity and quantity proves availability",
                      "M4-RECEIPT", owner="Buyer")
    outcome.update(group=group, validation=[group], status="Master association; receipt validation required",
                   receipt_controls=receipt_controls)
    return outcome


def receipt_capacities(rows, supplier_site, order, location, invoiced):
    """Only explicit receipt identities establish a balance; duplicate rows never multiply it."""
    capacities, known = {}, True
    for row in rows:
        parent = text(row.get("RMS_ITEM_ID"))
        if parent not in invoiced:
            continue
        receipt = text(row.get("RECEIPT_ID"))
        qty = to_decimal(row.get("QTY_RECEIVED"))
        if not receipt or qty is None or qty < 0:
            known = False
            continue
        key = "|".join(["master-receipt", text(row.get("EBS_SUPPLIER_CODE")).upper(), receipt, parent])
        if key in capacities and capacities[key]["qty"] != str(qty):
            known = False
        capacities[key] = {"key": key, "item": parent, "qty": str(qty), "reference": row.get("_ref", "")}
    known = known and bool(capacities) and all(q is not None and q >= 0 for q in invoiced.values()) and all(
        any(c["item"] == p for c in capacities.values()) for p in invoiced)
    return {"known": known, "capacities": list(capacities.values()),
            "scope": {"site": supplier_site, "po": order, "location": location},
            "demand": {p: str(q) if q is not None else None for p, q in invoiced.items()}}


def quantities_agree(rows, invoiced):
    """One distinct observed quantity per item may corroborate an order; repeats never add availability."""
    if not invoiced or any(v is None for v in invoiced.values()):
        return False
    observed = defaultdict(set)
    for row in rows:
        qty = to_decimal(row.get("QTY_RECEIVED"))
        if qty is not None:
            observed[text(row.get("RMS_ITEM_ID"))].add(qty)
    return all(observed.get(parent) == {qty} for parent, qty in invoiced.items())
