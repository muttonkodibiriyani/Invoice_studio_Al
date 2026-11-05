"""Evidenced joins between the master's independent blocks; no receipt balances are inferred."""
import re
from collections import defaultdict
from decimal import Decimal

from . import item_rules
from . import location_type as location
from .fine_rules import (_refs, ebs_key, evaluate_pogrn, location_market,
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
    # L4.1 / L4.2: the row's location name, typed by D6.
    found = {location.resolve(row["data"]) for row in rows}
    kinds = {f.type for f in found}
    configured = location_type(number, config)
    if any(location.ASK_LOCATION in f.prompts for f in found) or configured["conflict"] or len(kinds) != 1:
        return None
    kind = kinds.pop()
    if configured["type"] and kind != configured["type"]:
        return None
    return {"number": number, "rows": rows, "kind": kind, "kind_source": "Master location name (L4.2, D6)",
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
    rows = source.item_orders(parents, {supplier_site})
    groups = defaultdict(list)
    for row in rows:
        groups[text(row.get("RMS_ORDER_NO"))].append(row)
    groups.pop("", None)
    pick = choose_order(groups, parents, invoiced or {}, item_rules.rules().coverage_tiers())
    order, share = pick["order"], f"{pick['covered']} of {pick['total']} matched items"
    outcome["candidates"] = len(pick["tied"]) or int(order is not None)
    if order is None:
        run.exception("Missing PO", "No RMS order holds the matched items (P-PO-NF)", "O3.3", owner="Buyer")
        return outcome
    if pick["quantity_matched"] is False:
        run.exception("Quantity Mismatch", f"Tied orders: no order matches the invoiced quantity; {order} is the "
                      "nearest by QTY_RECEIVED, confirm or pick another (P-PO)", "D10", owner="Buyer")
    if pick["tied"]:
        run.exception("Ambiguous PO", f"Orders {', '.join(pick['tied'])} each cover {share} with the same invoice "
                      f"and received quantity; {order} is the lowest, confirm or pick another (P-PO)", "O3.3",
                      owner="Buyer")
    elif len(groups) > 1:
        # D3 + D2: the found items carry several orders; the chosen one is confirmed in review.
        run.exception("Ambiguous PO", f"The found items carry orders {', '.join(sorted(groups, key=lambda o: (len(o), o)))};"
                      f" {order} holds the most ({pick['decided']}), confirm or pick another (P-PO)", "O3.3",
                      owner="Buyer")
    if pick["tier"].export_mode == "no":
        run.exception("Missing PO", f"Order {order} covers only {share} ({pick['tier'].name}); add the missing "
                      "items to the item master, then Update item master", "O3.3", owner="Buyer")
        if not ORDER_NO_BELOW_TIER:
            return outcome
    elif pick["tier"].export_mode != "yes":
        run.exception("Ambiguous PO", f"Order {order} covers {share} ({pick['tier'].name}); confirm it (P-PO)",
                      "O3.3", owner="Buyer")
    selected = groups[order]
    # O3.2: the printed PO only validates the item-based order; a different printed PO is noted, never a failure.
    printed = text(run.invoice.po)
    printed_match = bool(printed) and any(printed in (order, text(r.get("MANUAL ORDER NO"))) for r in selected)
    # Every order row has an exact barcode/parent bridge to this supplier's item rows.
    order_refs = _refs(selected)
    run.trace("Order No", order, "O3.1" if pick["covered"] == pick["total"] else "O3.3",
              f"RMS_ORDER_NO on the found items' master rows (D3, {pick['decided']})" +
              ("" if printed_match or not printed else f"; invoice PO {printed} differs"),
              reference=order_refs, original=printed if printed_match else None)
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


# D3 (provisional, awaiting Tharak): Order No is never blank while a found item carries an order, so an order
# holding fewer found items than the workbook's lowest Coverage_Tiers row still fills it; the invoice stays in review.
ORDER_NO_BELOW_TIER = True


def order_rank(held, invoiced):
    """D2 (A4, confirmed 5 Nov): the most found items, then the most invoice quantity of those items."""
    return len(held), sum(to_decimal(invoiced.get(p)) or 0 for p in held)


def lowest_order(tied):
    """D3 (provisional, awaiting Tharak): a tie neither D2 nor D10 breaks goes to the lowest RMS_ORDER_NO."""
    return min(tied, key=lambda order: (len(order), order))


def choose_order(groups, parents, invoiced, tiers):
    """O3.1 / O3.3 with D2 / D3 / D10: the RMS order the found items (ITEM_PARENT) carry, ranked by order_rank; a tie
    goes to the order whose QTY_RECEIVED matches the invoice, else the nearest (received_gap); one still standing
    goes to lowest_order.

    ``groups`` is RMS_ORDER_NO -> order rows, ``invoiced`` ITEM_PARENT -> invoice quantity (None when unknown) and
    ``tiers`` RuleSet.coverage_tiers(). Returns ``{order, covered, total, tier, tied, decided}``: ``tied`` lists every
    tied order for the reviewer (P-PO) and ``decided`` says how the order was chosen: unique, items, quantity,
    received-exact, received-nearest or lowest-tie. ``quantity_matched`` is False when no tied order's QTY_RECEIVED
    matches the invoice (D10), None when received quantities did not decide. ``order`` is None only when no order
    holds a found item."""
    ranked = {}
    for order, group in groups.items():
        held = parents & {text(r.get("RMS_ITEM_ID")) for r in group}
        if held:
            ranked[order] = order_rank(held, invoiced)
    best = max(ranked.values(), default=None)
    tied = sorted((o for o, rank in ranked.items() if rank == best), key=lambda o: (len(o), o))
    runner_up = max((rank for rank in ranked.values() if rank != best), default=None)
    decided = ("lowest-tie" if len(tied) > 1 else "unique" if runner_up is None else
               "items" if runner_up[0] < best[0] else "quantity") if tied else None
    quantity_matched = None
    gaps = {o: received_gap(groups[o], parents, invoiced) for o in tied} if len(tied) > 1 else {}
    if gaps and None not in gaps.values():
        least = min(gaps.values())
        quantity_matched = least == 0
        tied = [o for o in tied if gaps[o] == least]
        if len(tied) == 1:
            decided = "received-exact" if quantity_matched else "received-nearest"
    order = lowest_order(tied) if tied else None
    covered = best[0] if best else 0
    pct = Decimal(100) * covered / len(parents) if parents else Decimal(0)
    tier = next((t for t in tiers if t.low <= pct and (t.high is None or pct < t.high)), None)
    return {"order": order, "covered": covered, "total": len(parents), "tier": tier,
            "tied": tied if len(tied) > 1 else [], "decided": decided, "quantity_matched": quantity_matched}


def received_gap(group, parents, invoiced):
    """D10 (Tharak, 5 Nov; provisional, awaiting Tharak): the total |invoice quantity - QTY_RECEIVED| over the found
    items an order holds, its rows of one item summed and a blank QTY_RECEIVED read as nothing received. None when
    an invoice quantity is unknown, so the received quantity cannot decide."""
    gap = Decimal(0)
    for parent in parents & {text(r.get("RMS_ITEM_ID")) for r in group}:
        wanted = to_decimal(invoiced.get(parent))
        if wanted is None:
            return None
        got = sum((to_decimal(r.get("QTY_RECEIVED")) or 0 for r in group if text(r.get("RMS_ITEM_ID")) == parent),
                  Decimal(0))
        gap += abs(wanted - got)
    return gap
