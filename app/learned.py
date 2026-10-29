"""Learn from verified answers.

A verified invoice (exported after review, or corrected and approved) teaches the
application two things, both kept in a private store under the data directory
(``<data>/learned``) and never in the repository:

* a supplier template for the local readers, in invoice2data syntax, synthesised from
  the verified values and the reader's own text and kept only when it reproduces them;
* short verified examples for the AI fallback prompt, plus owner-approved corrections
  from the fine-rules feedback log.

The store holds real invoice values. See docs/learning.md for provisioning, privacy
and the precedence rule that keeps learned templates behind the built-in readers.
"""
import difflib
import functools
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

STORE_DIR = "learned"
TEMPLATES_DIR = "templates"
SUPPLIERS_DIR = "suppliers"
CORRECTIONS_FILE = "corrections.json"
LEARNED_VERSION = 1
MAX_SAMPLES = 5
MAX_EXAMPLES = 3
MAX_CORRECTIONS = 5
EXAMPLE_LINES = 2
HEADER_FIELDS = ("number", "date", "currency", "net", "tax", "po")
LINE_FIELDS = ("sku", "gtin", "description", "qty", "uom", "price", "net_amount", "tax_amount")
EXACT_LINE_FIELDS = ("sku", "gtin", "qty", "uom", "price", "net_amount", "tax_amount")
NUMERIC = {"qty", "price", "net_amount", "tax_amount", "net", "tax"}
NUMBER = r"-?\d[\d,]*(?:\.\d+)?"
TOKEN_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])")
# The only files a reader child may hand back for the API process to write (LearnedStore.apply).
LEARNED_FILE = re.compile(r"(?:templates/[a-z0-9-]{1,60}\.yml|suppliers/[a-z0-9-]{1,60}\.json)")
GTIN = r"(?:[A-Za-z]{2,5}:?\s*)?(?P<gtin>\d{8,14})"
CONTINUATION = (r"^(?P<description>.*?)"
                r"(?:\s*,?\s*(?:[A-Za-z]{2,5}:?\s*)?(?<!\d)(?P<gtin>\d{8,14})(?!\d)(?P<tail>(?:\s.*)?)"
                r"|\s*,\s+(?P<stray>\d{1,7})|\s*,?)\s*$")
GTIN_SHAPE = r"(?:[A-Za-z]{2,5}:?\s*)?\d{8,14}"
TAIL_NUMERIC = re.compile(r"[\d\s.,/-]+")
NEVER = r"(?!)"
DATE_SHAPES = (
    ("%Y-%m-%d", r"\d{4}-\d{2}-\d{2}"),
    ("%d.%m.%Y", r"\d{2}\.\d{2}\.\d{4}"),
    ("%d/%m/%Y", r"\d{2}/\d{2}/\d{4}"),
    ("%d-%m-%Y", r"\d{2}-\d{2}-\d{4}"),
    ("%B %d, %Y", r"[A-Z][a-z]+ \d{1,2}, \d{4}"),
    ("%b %d, %Y", r"[A-Z][a-z]{2} \d{1,2}, \d{4}"),
    ("%d %B %Y", r"\d{1,2} [A-Z][a-z]+ \d{4}"),
    ("%d %b %Y", r"\d{1,2} [A-Za-z]{3} \d{4}"),
    ("%d-%b-%Y", r"\d{1,2}-[A-Za-z]{3}-\d{4}"),
    ("%d-%B-%Y", r"\d{1,2}-[A-Za-z]{4,9}-\d{4}"),
    ("%d/%m/%y", r"\d{2}/\d{2}/\d{2}"),
    ("%d.%m.%y", r"\d{2}\.\d{2}\.\d{2}"),
)


# ---------------------------------------------------------------- values

def _text(value):
    return "" if value is None else " ".join(str(value).split())


def _dec(value):
    try:
        parsed = Decimal(_text(value).replace(",", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _same(field, verified, produced):
    left, right = _text(verified), _text(produced)
    if not left:
        return not right
    if field in NUMERIC:
        return _dec(left) is not None and _dec(left) == _dec(right)
    return left.casefold() == right.casefold()


def _near(left, right):
    left, right = _text(left).casefold(), _text(right).casefold()
    return bool(left and right) and difflib.SequenceMatcher(None, left, right).ratio() >= 0.9


def _squash(value):
    return re.sub(r"[\s,]+", "", _text(value)).casefold()


def _plain_number(value):
    value = _text(value)
    return value.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", value) else value


def plain_invoice(invoice):
    """Verified values as plain strings: header fields plus the fields of each line."""
    if hasattr(invoice, "model_dump"):
        invoice = invoice.model_dump(mode="json")
    invoice = invoice or {}
    header = {field: _text(invoice.get(field)) for field in HEADER_FIELDS + ("supplier_name", "buyer_name")}
    lines = []
    for line in invoice.get("lines") or []:
        row = {field: _text((line or {}).get(field)) for field in LINE_FIELDS}
        if any(row.values()):
            lines.append(row)
    header["lines"] = lines
    return header


def supplier_key(name):
    slug = re.sub(r"[^a-z0-9]+", "-", _text(name).casefold()).strip("-")
    return slug[:60] or "unknown"


# ---------------------------------------------------------------- regex pieces

def _escape_token(token, exact_digits=False):
    """Escape a printed token; digit runs and repeated punctuation generalise."""
    if len(token) > 2 and len(set(token)) == 1 and not token.isalnum():
        return re.escape(token[0]) + "+"
    if exact_digits:
        return re.sub(r"\d+", lambda m: r"\d{%d}" % len(m.group()), re.escape(token))
    if re.fullmatch(NUMBER, token.rstrip(",;:")):
        return NUMBER + re.escape(token[len(token.rstrip(",;:")):])
    return re.sub(r"\d+", r"\\d+", re.escape(token))


def _skip_pattern(line):
    """A full-line pattern for a noise line; digit runs keep their length so barcodes never match."""
    return "^" + r"\s+".join(_escape_token(token, exact_digits=True) for token in line.split()) + "$"


def _phrase(tokens):
    return r"\s+".join(_escape_token(token) for token in tokens)


def _shape(value):
    out = []
    for match in re.finditer(r"\d+|[A-Z]+|[a-z]+|.", value):
        token = match.group()
        if token[0].isdigit():
            out.append(r"\d+")
        elif token.isupper():
            out.append("[A-Z]+")
        elif token.islower():
            out.append("[a-z]+")
        else:
            out.append(re.escape(token))
    return "".join(out)


def _line_prefix(line, limit=3):
    """Leading tokens without digits; the full generalised line when it starts with a number."""
    tokens = line.split()
    lead = []
    for token in tokens[:limit]:
        if any(char.isdigit() for char in token):
            break
        lead.append(token)
    return _phrase(lead) if lead else _phrase(tokens[:limit])


def _printed(lines, field, value):
    """Where a verified value is printed: (line index, start, end, value patterns, extra)."""
    for index, line in enumerate(lines):
        if field in ("net", "tax"):
            target = _dec(value)
            for match in TOKEN_NUMBER.finditer(line):
                if target is not None and _dec(match.group()) == target:
                    decimals = len(match.group().split(".")[1]) if "." in match.group() else 0
                    pattern = r"-?\d[\d,]*\.\d{%d}" % decimals if decimals else r"-?\d[\d,]*"
                    yield index, match.start(), match.end(), [pattern], {"type": "float", "decimals": decimals}
        elif field == "date":
            for date_format, pattern in DATE_SHAPES:
                for match in re.finditer(pattern, line):
                    try:
                        parsed = datetime.strptime(match.group(), date_format)
                    except ValueError:
                        continue
                    if parsed.date().isoformat() == value:
                        yield index, match.start(), match.end(), [pattern], {"date_format": date_format}
        else:
            for match in re.finditer(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", line):
                yield index, match.start(), match.end(), [r"\S+", _shape(value)], {}


def _all_numbers(tokens):
    """True when every token is number-shaped (digits with separators), such as an invoice number or a date."""
    return all(any(c.isdigit() for c in t) and not any(c.isalpha() for c in t) for t in tokens)


def _header_spec(text, lines, field, value):
    """Regexes that find the verified value exactly once, anchored on printed text.

    Returns (regex, extra, fallbacks). Anchors are ranked by how well they travel to sibling invoices:
    a label on the same line, a label on the line above (first with the value first on its line, then
    counting the tokens before it), then a bare number before the value (siblings print other number-value
    pairs); anchors that find the value more than once but always the same (a header repeated on every
    page) come after, in the same order. The reader tries the fallbacks in order when the first regex
    finds no single value. Token-counting anchors are never fallbacks: a fallback runs when the first
    anchor failed, so the wording differs, and then a token count lands on some other token just as surely.
    """
    kinds = ("label", "above", "above-skip", "number", "number-above")
    ranked = {kind: [] for kind in kinds}
    ranked.update({"repeated-" + kind: [] for kind in kinds})
    for index, start, end, patterns, extra in _printed(lines, field, value):
        line = lines[index]
        before = line[:start]
        printed = line[start:end]
        anchors = []
        if before.strip():
            tokens = before.split()
            # The value sits on the label's line: a gap that crossed lines could pick up the next line's text.
            gap = r"[ \t]*" if not before[-1].isspace() or not tokens[-1][-1].isalnum() else r"[ \t]+"
            for count in range(1, min(6, len(tokens)) + 1):
                label = "".join(tokens[-count:])
                if label.isalpha() and len(label) < 3:
                    continue  # "in", "of": too weak to anchor on alone
                kind = "number" if _all_numbers(tokens[-count:]) else "label"
                anchors.append((_phrase(tokens[-count:]) + gap, kind, False))
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0:
            tokens = lines[previous].split()
            position = len(before.split())
            skip = r"(?:\S+[ \t]+){%d}" % position if position else ""  # stay on the value's line
            for count in range(1, min(4, len(tokens)) + 1):
                kind = "number-above" if _all_numbers(tokens[-count:]) else "above" if not position else "above-skip"
                anchors.append((_phrase(tokens[-count:]) + r"\s*\n\s*" + skip, kind, position > 0))
        for anchor, kind, counts_tokens in anchors:
            for pattern in patterns:
                regex = anchor + "(" + pattern + ")"
                try:
                    found = re.findall(regex, text)
                except re.error:
                    continue
                if not found or any(item != printed for item in found):
                    continue
                bucket = ranked[kind if len(found) == 1 else "repeated-" + kind]
                if all(regex != known for known, _, _ in bucket):
                    bucket.append((regex, extra, counts_tokens))
    ordered = [bucket[0] for bucket in ranked.values() if bucket]  # the best anchor of each kind
    if not ordered:
        return None
    regex, extra, _ = ordered[0]
    fallbacks = [{"regex": other, **({"date_format": more["date_format"]} if more.get("date_format") else {})}
                 for other, more, counts_tokens in ordered[1:] if not counts_tokens][:2]
    return regex, extra, fallbacks


# ---------------------------------------------------------------- line rows

def _numeric(token):
    token = token.strip(",;")
    if token.endswith("%"):
        return None
    return _dec(token) if re.fullmatch(NUMBER, token) else None


def _locate_rows(lines, rows):
    anchors = []
    position = 0
    for number, row in enumerate(rows, 1):
        needed = [_dec(row.get(field)) for field in ("qty", "price", "net_amount")]
        needed = [value for value in needed if value is not None]
        if len(needed) < 2:
            return None, f"line {number} has too few numbers to locate"
        for index in range(position, len(lines)):
            printed = [_numeric(token) for token in lines[index].split()]
            if all(any(value == need for value in printed if value is not None) for need in needed):
                anchors.append(index)
                position = index + 1
                break
        else:
            return None, f"line {number} was not found in the text"
    return anchors, None


def _assign_numbers(clean, roles, row):
    """Place qty, price, amount and tax in the usual column order, each left of the next.

    Scans from the right so a repeated amount (sub-total after a zero discount, qty 1 with
    price equal to the amount) lands on the same column in every row. None when that order
    does not hold; the caller then falls back to matching by value alone.
    """
    wanted = [(field, _dec(row.get(field))) for field in ("qty", "price", "net_amount", "tax_amount")]
    wanted = [(field, value) for field, value in wanted if value is not None]
    positions = {}
    limit = len(clean)
    for field, value in reversed(wanted):
        hits = [k for k in range(limit) if roles[k] is None and _numeric(clean[k]) == value]
        if not hits:
            return None
        exact = [k for k in hits if _plain_number(clean[k]) == _plain_number(row.get(field))]
        positions[field] = (exact or hits)[-1]
        limit = positions[field]
    return positions


def _assign_roles(line, row, ordinal):
    """Name each token of a printed row: serial, sku, description, gtin, uom, numbers, extra."""
    tokens = line.split()
    clean = [token.strip(",;") for token in tokens]
    roles = [None] * len(tokens)
    meta = {}
    if tokens and re.fullmatch(r"\d+", tokens[0]) and int(tokens[0]) == ordinal:
        roles[0] = "serial"
    positions = _assign_numbers(clean, roles, row)
    if positions:
        for field, k in positions.items():
            roles[k] = field
    else:
        for field in ("net_amount", "price", "tax_amount", "qty"):
            target = _dec(row.get(field))
            if target is None:
                continue
            hits = [k for k, token in enumerate(clean) if roles[k] is None and _numeric(token) == target]
            if not hits:
                return None, meta, f"{field} is not printed on its row"
            exact = [k for k in hits if _plain_number(clean[k]) == _plain_number(row.get(field))]
            roles[(exact or hits)[-1 if field == "qty" else 0]] = field
    sku = _text(row.get("sku"))
    if sku:
        hits = [k for k, token in enumerate(clean) if roles[k] is None and token == sku]
        if hits:
            roles[hits[0]] = "sku"
        else:
            for k in range(len(clean) - 1):
                if roles[k] is not None or roles[k + 1] is not None:
                    continue
                left, right = clean[k], clean[k + 1]
                if left + right == sku:
                    join, first, second = "ab", left, right
                elif right + left == sku:
                    join, first, second = "ba", right, left
                else:
                    continue
                if first.endswith("-") and not second.endswith("-"):
                    join = "dash"
                roles[k], roles[k + 1], meta["sku_join"] = "sku", "sku_part", join
                break
    description = _squash(row.get("description"))
    gtin = _text(row.get("gtin"))
    if gtin:
        for k, token in enumerate(clean):
            if roles[k] is None and token.endswith(gtin) and not token[:-len(gtin)].strip(":").isdigit():
                roles[k] = "gtin"
                label = clean[k - 1] if k else ""
                if (k and roles[k - 1] is None and re.fullmatch(r"[A-Za-z]{2,5}:?", label)
                        and not description.endswith(_squash(label))):
                    roles[k - 1] = "gtin_label"
                break
    uom = _text(row.get("uom"))
    if uom:
        for k, token in enumerate(clean):
            if roles[k] is None and token.casefold() == uom.casefold():
                roles[k] = "uom"
                break
    best = (0, None)
    k = 0
    while k < len(tokens):
        if roles[k] is not None:
            k += 1
            continue
        run_start = k
        matched = 0
        prefix = ""
        while k < len(tokens) and roles[k] is None:
            prefix += _squash(tokens[k])
            if description and description.startswith(prefix) and prefix:
                matched = k - run_start + 1
            k += 1
        if matched > best[0]:
            best = (matched, run_start)
    if best[0]:
        for k in range(best[1], best[1] + best[0]):
            roles[k] = "description"
    return [(role or "extra", token) for role, token in zip(roles, tokens)], meta, None


def _sequence(roles):
    """Compress a role list into (role, None) items and ('extra', [tokens]) runs."""
    out = []
    for role, token in roles:
        if role in ("sku_part", "gtin_label"):
            continue
        if role == "extra":
            if out and out[-1][0] == "extra":
                out[-1][1].append(token)
            else:
                out.append(("extra", [token]))
        else:
            out.append((role, None))
    return out


def _gap_unit(gap_lists):
    counts = [len(gap) for gap in gap_lists]
    unit = {"kind": "extra", "min": min(counts), "max": max(counts)}
    texts = [" ".join(gap) for gap in gap_lists if gap]
    if texts and all(re.fullmatch(GTIN_SHAPE, text) for text in texts):
        unit["pattern"] = GTIN_SHAPE
    return unit


def _merge_sequences(sequences):
    """Canonical column order with optional roles and bounded extra runs; None when rows disagree."""
    order = []
    for sequence in sequences:
        last = -1
        for role, _ in sequence:
            if role == "extra":
                continue
            if role in order:
                position = order.index(role)
                if position < last:
                    return None
                last = position
            else:
                order.insert(last + 1, role)
                last += 1
    present = {role: 0 for role in order}
    gaps = {}
    for sequence in sequences:
        roles = [role for role, _ in sequence if role != "extra"]
        for role in roles:
            present[role] += 1
        found = {}
        current = "start"
        extra = []
        for role, tokens in sequence:
            if role == "extra":
                extra.extend(tokens)
            else:
                found[(current, role)] = extra
                current, extra = role, []
        found[(current, "end")] = extra
        chain = ["start"] + order + ["end"]
        for left, right in zip(chain, chain[1:]):
            gaps.setdefault((left, right), []).append(found.get((left, right), []))
    units = []
    chain = ["start"] + order + ["end"]
    for left, right in zip(chain, chain[1:]):
        gap_lists = gaps.get((left, right), [[]])
        if any(gap_lists):
            units.append(_gap_unit(gap_lists))
        if right != "end":
            units.append({"kind": "role", "name": right, "optional": present[right] < len(sequences)})
    return units


def _role_core(name, last):
    if name == "serial":
        return r"\d+"
    if name == "sku":
        return r"(?P<sku>\S+)"
    if name == "sku2":
        return r"(?P<sku>\S+\s+\S+)"
    if name == "description":
        return r"(?P<description>.+)" if last else r"(?P<description>.+?)"
    if name == "gtin":
        return GTIN
    if name == "gtin_plain":
        return r"(?P<gtin>\d{8,14})"
    if name == "uom":
        return r"(?P<uom>[A-Za-z.]{1,8})"
    return r"(?P<%s>%s)" % (name, NUMBER)


def _row_regex(units, sku_parts, gtin_label=True):
    out = "^"
    floating = False  # a barcode printed under the name lands on either side of it in OCR reading order
    for index, unit in enumerate(units):
        last = index == len(units) - 1
        if unit["kind"] == "extra":
            if unit.get("pattern"):
                piece = r"(?:\s+%s)" % unit["pattern"] if last else r"(?:%s\s+)" % unit["pattern"]
                out += piece + ("?" if unit["min"] == 0 or floating else "")
                floating = False
            else:
                out += (r"(?:\s+\S+){%d,%d}" if last else r"(?:\S+\s+){%d,%d}") % (unit["min"], unit["max"])
            continue
        name = "sku2" if unit["name"] == "sku" and sku_parts == 2 else unit["name"]
        if name == "gtin" and not gtin_label:
            name = "gtin_plain"
        core = _role_core(name, last)
        optional = unit["optional"] or name == "serial"
        following = units[index + 1] if not last else None
        separator = "" if following and following["kind"] == "extra" and index + 1 == len(units) - 1 else r"\s+"
        if name == "description" and following and following["kind"] == "extra" and following.get("pattern"):
            out += r"(?:%s\s+)?" % following["pattern"]
            floating = True
        if last:
            out += "(?:%s)?" % core if optional else core
        else:
            out += "(?:%s%s)?" % (core, separator) if optional else core + separator
    return out + r"\s*$"


def _continuations(lines, anchors, end_index, rows, roles_by_row):
    """Which lines between rows carry description, code or barcode text; the rest is noise.

    Also learns what happens to text printed after a barcode on the same line (kept, dropped,
    or kept only when it is not a bare number).
    """
    needed = set()
    noise = []
    kept_tails, dropped_tails = [], []
    for n, anchor in enumerate(anchors):
        stop = anchors[n + 1] if n + 1 < len(anchors) else end_index
        on_row = "".join(_squash(token) for role, token in roles_by_row[n] if role == "description")
        remaining = _squash(rows[n].get("description"))[len(on_row):]
        gtin = _text(rows[n].get("gtin"))
        gtin_done = gtin and any(role == "gtin" for role, _ in roles_by_row[n])
        for index in range(anchor + 1, stop):
            line = lines[index]
            if not line.strip():
                continue
            match = re.match(CONTINUATION, line)
            part = _squash(match.group("description")) if match else ""
            has_gtin = bool(gtin) and bool(match and match.group("gtin") == gtin)
            tail = (match.group("tail") or match.group("stray") or "").strip() if match else ""
            if part and remaining.startswith(part):
                remaining = remaining[len(part):]
                needed.add(index)
            elif has_gtin and not gtin_done:
                needed.add(index)
            else:
                noise.append(index)
                continue
            if has_gtin:
                gtin_done = True
            if tail:
                squashed = _squash(tail)
                if remaining.startswith(squashed):
                    remaining = remaining[len(squashed):]
                    kept_tails.append(tail)
                else:
                    dropped_tails.append(tail)
    if not dropped_tails:
        tail_mode = "all" if kept_tails else "none"
    elif not kept_tails:
        tail_mode = "none"
    else:
        tail_mode = "text"
    return needed, noise, tail_mode


def _learn_lines(lines, rows, report):
    anchors, problem = _locate_rows(lines, rows)
    if problem:
        report["lines"] = problem
        return None, None
    roles_by_row = []
    meta = {}
    for n, (anchor, row) in enumerate(zip(anchors, rows), 1):
        roles, row_meta, problem = _assign_roles(lines[anchor], row, n)
        if problem:
            report["lines"] = f"line {n}: {problem}"
            return None, None
        roles_by_row.append(roles)
        meta.update(row_meta)
    units = _merge_sequences([_sequence(roles) for roles in roles_by_row])
    if units is None:
        report["lines"] = "rows do not share one column order"
        return None, None
    sku_parts = 2 if "sku_join" in meta else 1
    gtin_label = any(role == "gtin_label" for roles in roles_by_row for role, _ in roles)
    first_line = _row_regex(units, sku_parts, gtin_label)
    header_index = anchors[0] - 1
    while header_index >= 0 and not lines[header_index].strip():
        header_index -= 1
    if header_index < 0:
        report["lines"] = "no header line above the first row"
        return None, None
    start = _line_prefix(lines[header_index])
    last_needed = anchors[-1]
    needed, noise, tail_mode = _continuations(lines, anchors, len(lines), rows, roles_by_row)
    for index in needed:
        if index > last_needed:
            last_needed = index
    end_index = next((index for index in range(last_needed + 1, len(lines)) if lines[index].strip()), None)
    if end_index is None:
        report["lines"] = "no line after the last row"
        return None, None
    ends = [_line_prefix(lines[end_index])]
    skips = []
    noise = [index for index in noise if index < end_index]
    start_regex = re.compile(start)
    index = 0
    while index < len(noise):
        line_index = noise[index]
        following = next((a for a in anchors if a > line_index), end_index)
        if any(start_regex.search(lines[k]) for k in range(line_index + 1, following)):
            pattern = _line_prefix(lines[line_index])
            if pattern not in ends:
                ends.append(pattern)
            while index < len(noise) and noise[index] < following:
                index += 1
            continue
        pattern = _skip_pattern(lines[line_index])
        if pattern not in skips:
            skips.append(pattern)
        index += 1
    continuation = any(index > anchors[0] for index in needed)
    rule = {"start": start, "end": "|".join(ends), "first_line": first_line,
            "line": CONTINUATION if continuation else NEVER}
    if skips:
        rule["skip_line"] = skips
    constants = {}
    uoms = {_text(row.get("uom")) for row in rows}
    if len(uoms) == 1 and next(iter(uoms)) and not any(role == "uom" for roles in roles_by_row for role, _ in roles):
        constants["uom"] = next(iter(uoms))
    meta.update(continuation=continuation, constants=constants, gtin_tail=tail_mode,
                header_line=lines[header_index])
    return {"parser": "lines", "rules": [rule]}, meta


# ---------------------------------------------------------------- template synthesis

def _keywords(text, supplier_name):
    words = _text(supplier_name).split()
    for count in range(len(words), 1, -1):
        match = re.search(r"\s+".join(re.escape(word) for word in words[:count]), text, re.IGNORECASE)
        if match:
            return [match.group(0)]
    if len(words) == 1:
        match = re.search(r"(?<!\w)" + re.escape(words[0]) + r"(?!\w)", text, re.IGNORECASE)
        if match:
            return [match.group(0)]
    return []


def identity(text, invoice, baseline=None, header_line=""):
    """Who printed this: (supplier name, store key, keywords) from the verified name, the reader's
    name, or failing both the table header this layout prints above its rows."""
    for candidate in (plain_invoice(invoice)["supplier_name"], _text((baseline or {}).get("supplier_name"))):
        keywords = _keywords(text, candidate) if candidate else []
        if keywords:
            return candidate, supplier_key(candidate), keywords
    tokens = header_line.split()[:3]
    if len(tokens) >= 2:
        match = re.search(r"\s+".join(re.escape(token) for token in tokens), text)
        if match:
            return "", "layout-" + supplier_key(" ".join(tokens)), [match.group(0)]
    return "", "", []


def learn_template(text, invoice, source_id="", baseline=None):
    """Synthesise an invoice2data template that reproduces the verified invoice from this text.

    Returns (template, report); the template is None when the text does not support one.
    """
    verified = plain_invoice(invoice)
    report = {"source_id": source_id, "header": {}, "lines": None, "identity": identity(text, invoice, baseline)}
    lines = text.split("\n")
    rows = verified["lines"]
    if not rows:
        report["reason"] = "verified invoice has no lines"
        return None, report
    lines_field, meta = _learn_lines(lines, rows, report)
    if lines_field is None:
        report["reason"] = report["lines"]
        return None, report
    name, key, keywords = identity(text, invoice, baseline, meta.get("header_line", ""))
    report["identity"] = (name, key, keywords)
    if not keywords:
        report["reason"] = "neither a supplier name nor a table header identifies this layout"
        return None, report
    fields = {}
    options = {"currency": verified["currency"] or "EUR", "date_formats": []}
    fallbacks, decimals = {}, {}
    for field, target in (("number", "invoice_number"), ("date", "date"), ("net", "amount"),
                          ("tax", "tax"), ("po", "po"), ("currency", "currency")):
        value = verified.get(field)
        if not value:
            continue
        spec = _header_spec(text, lines, field, value)
        if spec is None:
            report["header"][field] = "no unique printed anchor"
            continue
        regex, extra, more = spec
        if more:
            fallbacks[field] = more
        if extra.get("decimals") is not None:
            decimals[field] = extra["decimals"]
        if extra.get("type"):
            fields[target] = {"parser": "regex", "regex": regex, "type": extra["type"]}
        else:
            fields[target] = regex
        if extra.get("date_format"):
            options["date_formats"].append(extra["date_format"])
        report["header"][field] = "anchored"
    if "invoice_number" in fields:
        label = re.sub(r"\\s[+*]", " ", fields["invoice_number"].split("(")[0])
        label = re.sub(r"\\(.)", r"\1", label).replace("\\d+", "").strip()
        if len(label) >= 3 and "\n" not in label and label in text and label not in keywords:
            keywords.append(label)
    if name:
        fields["static_supplier_name"] = name
    fields["lines"] = lines_field
    template = {
        "issuer": name or key,
        "keywords": keywords,
        "fields": fields,
        "required_fields": ["lines"],
        "options": options,
        "learned": {
            "version": LEARNED_VERSION,
            "supplier_key": key,
            "source_id": source_id,
            "learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "continuation": meta.get("continuation", False),
            "constants": meta.get("constants", {}),
            "confirmed": [],
        },
    }
    if fallbacks:
        template["learned"]["fallbacks"] = fallbacks
    if decimals:
        template["learned"]["decimals"] = decimals
    if meta.get("sku_join"):
        template["learned"]["sku_join"] = meta["sku_join"]
    if meta.get("gtin_tail") and meta["gtin_tail"] != "none":
        template["learned"]["gtin_tail"] = meta["gtin_tail"]
    ok, fidelity = reproduces(text, template, invoice)
    template["learned"]["fidelity"] = fidelity
    report["fidelity"] = fidelity
    if not ok:
        report["reason"] = "template does not reproduce the verified answer"
        return None, report
    template["learned"]["fixes"] = fixes(verified, baseline, fidelity) if baseline is not None else []
    return template, report


def finish_row(row, meta):
    """Clean a learned template's row: joined description parts, one barcode, plain numbers."""
    meta = meta or {}
    out = {}
    parts = [part.strip().rstrip(",;").strip() for part in str(row.get("description") or row.get("name") or "").split("\n")]
    mode = meta.get("gtin_tail", "none")
    for tail in str(row.get("tail") or "").split("\n") + str(row.get("stray") or "").split("\n"):
        tail = tail.strip().strip(",;").strip()
        if tail and (mode == "all" or (mode == "text" and not TAIL_NUMERIC.fullmatch(tail))):
            parts.append(tail)
    out["description"] = " ".join(part for part in parts if part)
    gtins = [re.sub(r"\D", "", part) for part in str(row.get("gtin") or "").split("\n")]
    out["gtin"] = next((gtin for gtin in gtins if gtin), "")
    skus = [part.strip() for part in str(row.get("sku") or "").split("\n") if part.strip()]
    if skus:
        sku = skus[0].rstrip(",;")
        tokens = sku.split()
        if meta.get("sku_join") and len(tokens) == 2:
            if meta["sku_join"] == "dash":
                tokens = sorted(tokens, key=lambda token: not token.endswith("-"))
            elif meta["sku_join"] == "ba":
                tokens = tokens[::-1]
            sku = "".join(tokens)
        out["sku"] = sku
    for field in ("qty", "price", "net_amount", "tax_amount"):
        value = str(row.get(field) or "").split("\n")[0].strip()
        if value:
            out[field] = _plain_number(value)
    uom = str(row.get("uom") or "").split("\n")[0].strip()
    out["uom"] = uom or (meta.get("constants") or {}).get("uom", "")
    return {field: value for field, value in out.items() if value not in (None, "")}


def apply_template(text, template):
    """Read text with one learned template through the production worker path."""
    from .ocr_worker import template_extract, templates_from
    with tempfile.TemporaryDirectory(prefix="learned-") as folder:
        name = (template.get("learned") or {}).get("supplier_key") or "learned"
        Path(folder, name + ".yml").write_text(
            yaml.safe_dump(template, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return template_extract(text, templates_from([folder]))


def compare(verified, produced):
    """Fidelity of a produced reading against verified values (header per field, lines per row)."""
    verified = plain_invoice(verified)
    produced = plain_invoice(produced or {})
    fidelity = {"header": {}, "lines": len(verified["lines"]), "produced": len(produced["lines"]),
                "exact": 0, "near": 0, "line_fields": {}}
    for field in HEADER_FIELDS:
        if verified[field]:
            fidelity["header"][field] = "exact" if _same(field, verified[field], produced[field]) else "miss"
    for field in LINE_FIELDS:
        fidelity["line_fields"][field] = 0
    for expected, actual in zip(verified["lines"], produced["lines"]):
        exact = True
        for field in LINE_FIELDS:
            if _same(field, expected[field], actual[field]):
                fidelity["line_fields"][field] += 1
            elif field == "description" and _near(expected[field], actual[field]):
                fidelity["line_fields"][field] += 1
                exact = False
            else:
                exact = None if exact is None else False
        if exact:
            fidelity["exact"] += 1
        elif exact is False:
            fidelity["near"] += 1
    fidelity["ok"] = (
        fidelity["lines"] == fidelity["produced"]
        and all(value == "exact" for value in fidelity["header"].values())
        and fidelity["exact"] + fidelity["near"] == fidelity["lines"]
        and all(fidelity["line_fields"][field] == fidelity["lines"] for field in LINE_FIELDS)
    )
    return fidelity


def reproduces(text, template, invoice):
    try:
        produced = apply_template(text, template)
    except Exception as error:
        return False, {"error": type(error).__name__}
    fidelity = compare(invoice, produced)
    return fidelity["ok"], fidelity


def fixes(verified, baseline, fidelity):
    """Fields the built-in reading got wrong on the verified invoice and the template gets right."""
    verified = plain_invoice(verified)
    baseline = plain_invoice(baseline or {})
    wrong = []
    for field in HEADER_FIELDS:
        if verified[field] and fidelity["header"].get(field) == "exact" and not _same(field, verified[field], baseline[field]):
            wrong.append(field)
    # A field the verified invoice never fills (a supplier whose invoices print no SKU column) proves nothing.
    line_fields = [field for field in LINE_FIELDS if any(line[field] for line in verified["lines"])]
    if len(baseline["lines"]) != len(verified["lines"]):
        wrong.extend(field for field in line_fields if fidelity["line_fields"].get(field) == fidelity["lines"])
        wrong.append("rows")
        return wrong
    for field in line_fields:
        if fidelity["line_fields"].get(field) != fidelity["lines"]:
            continue
        if any(not _same(field, expected[field], actual[field]) for expected, actual in zip(verified["lines"], baseline["lines"])):
            wrong.append(field)
    return wrong


# ---------------------------------------------------------------- private store

def _locked(method):
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return run


class LearnedStore:
    """Private per-supplier memory under ``<data>/learned``; persist/remove mirror files to cloud storage."""

    def __init__(self, data_root, persist=None, remove=None):
        self.root = Path(data_root) / STORE_DIR
        self.persist = persist
        self.remove = remove
        self.pending = None  # a list: writes are collected for apply() in another process, not made
        # One writer at a time in the API process: each change reads a supplier's file, changes it and writes it
        # whole. learn_from_job holds this across its reader child too, so a forget cannot be undone by a child
        # that read the store before it.
        self.lock = threading.RLock()
        for name in (TEMPLATES_DIR, SUPPLIERS_DIR):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)  # the store holds real invoice values: owner-only, like its files

    @property
    def templates_dir(self):
        return self.root / TEMPLATES_DIR

    @_locked
    def apply(self, writes):
        """Write the files a reader child's record_verified collected: supplier and template files only."""
        for name, content in writes:
            if not LEARNED_FILE.fullmatch(name):
                raise ValueError("A learned file is outside the store")
            self._write(self.root / name, content)

    def _write(self, path, content):
        if self.pending is not None:
            self.pending.append((path.relative_to(self.root).as_posix(), content))
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".part-" + uuid.uuid4().hex)
        try:
            # A supplier name can be printed in any script, so the store is written and read as
            # UTF-8 rather than in whatever code page happened to start the app.
            temporary.write_text(content, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if self.persist:
            self.persist(path)

    def _delete(self, path):
        if path.exists():
            path.unlink()
            if self.remove:
                self.remove(path)

    def _supplier_path(self, key):
        return self.root / SUPPLIERS_DIR / (key + ".json")

    def _template_path(self, key):
        return self.templates_dir / (key + ".yml")

    def _load_supplier(self, key):
        path = self._supplier_path(key)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _load_template(self, key):
        path = self._template_path(key)
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None

    def _save_template(self, key, template):
        self._write(self._template_path(key), yaml.safe_dump(template, sort_keys=False, allow_unicode=True))

    def _suppliers(self):
        for path in sorted((self.root / SUPPLIERS_DIR).glob("*.json")):
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

    def _corrections(self):
        path = self.root / CORRECTIONS_FILE
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _save_corrections(self, entries):
        self._write(self.root / CORRECTIONS_FILE, json.dumps(entries, indent=1))

    def _match(self, text):
        """The stored supplier whose keywords all appear in this text, if any."""
        for data in self._suppliers():
            keywords = data.get("keywords") or []
            if keywords and all(keyword in text for keyword in keywords):
                return data
        return None

    @_locked
    def record_verified(self, invoice, text, source_id, origin="export", baseline=None):
        """Remember a verified invoice and learn or confirm its supplier's template."""
        verified = plain_invoice(invoice)
        text = text or ""
        name, key, keywords = identity(text, invoice, baseline)
        data = self._load_supplier(key) if key else None
        if data is None:
            data = self._match(text)
        if data is not None:
            key = data["supplier_key"]
            name = name or data.get("supplier_name", "")
            keywords = keywords or data.get("keywords", [])
        sample = {"source_id": str(source_id), "origin": origin,
                  "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "invoice": verified, "text": text}
        samples = [item for item in (data or {}).get("samples", []) if item["source_id"] != sample["source_id"]]
        samples.append(sample)
        outcome = self._learn(key, sample, samples, baseline)
        key = outcome.get("supplier_key") or key
        if not key:
            return {"supplier_key": None, "template": "none", "reason": outcome.get("reason"), "examples": 0}
        name = name or outcome.get("supplier_name", "")
        keywords = keywords or outcome.get("keywords", [])
        data = data or {"supplier_key": key, "samples": []}
        keep = (outcome.get("source_id"),)
        data["samples"] = [item for item in samples[-MAX_SAMPLES:]] + [
            item for item in samples[:-MAX_SAMPLES] if item["source_id"] in keep]
        data["supplier_key"] = key
        data["supplier_name"] = name
        data["keywords"] = keywords
        data["template"] = {field: value for field, value in outcome.items()
                            if field not in ("supplier_name", "keywords")}
        self._write(self._supplier_path(key), json.dumps(data, indent=1))
        return {"supplier_key": key, "template": outcome["status"], "reason": outcome.get("reason"),
                "examples": len(data["samples"]), "fixes": outcome.get("fixes", [])}

    def _learn(self, key, sample, samples, baseline):
        existing = self._load_template(key) if key else None
        meta = (existing or {}).get("learned") or {}
        if existing and meta:
            ok, _ = reproduces(sample["text"], existing, sample["invoice"])
            if ok:
                meta["confirmed"] = sorted(set(meta.get("confirmed", [])) | {sample["source_id"]})
                meta.pop("failed_on", None)
                self._save_template(key, existing)
                return {"status": "confirmed", "source_id": meta.get("source_id"), "confirmed": meta["confirmed"],
                        "fixes": meta.get("fixes", [])}
        template, report = learn_template(sample["text"], sample["invoice"], sample["source_id"], baseline)
        name, found_key, keywords = report.get("identity") or ("", "", [])
        if template is None:
            status = "stale" if existing else "none"
            return {"status": status, "reason": report.get("reason"), "source_id": meta.get("source_id"),
                    "supplier_key": key or found_key, "supplier_name": name, "keywords": keywords}
        key = key or found_key
        template["learned"]["supplier_key"] = key
        others = [item for item in samples if item["source_id"] != sample["source_id"]]
        failed = [item["source_id"] for item in others if not reproduces(item["text"], template, item["invoice"])[0]]
        template["learned"]["confirmed"] = [item["source_id"] for item in others if item["source_id"] not in failed]
        if failed and existing:
            return {"status": "stale", "reason": "a new template would not reproduce earlier verified invoices",
                    "source_id": meta.get("source_id"), "failed_on": failed}
        if failed:
            template["learned"]["failed_on"] = failed
        self._save_template(key, template)
        return {"status": "relearned" if existing else "learned", "source_id": sample["source_id"],
                "fixes": template["learned"].get("fixes", []), "fidelity": template["learned"].get("fidelity"),
                "supplier_key": key, "supplier_name": name, "keywords": template["keywords"]}

    def prompt_examples(self, text, limit=MAX_EXAMPLES):
        """Verified examples and approved corrections for the supplier that printed this text."""
        text = text or ""
        blocks = []
        matched = []
        for data in self._suppliers():
            keywords = data.get("keywords") or _keywords(text, data.get("supplier_name"))
            if not keywords or not all(keyword in text for keyword in keywords):
                continue
            matched.append(data["supplier_key"])
            for sample in list(reversed(data.get("samples", [])))[:limit]:
                invoice = sample.get("invoice") or {}
                example = {field: invoice.get(field) for field in HEADER_FIELDS if invoice.get(field)}
                example["rows"] = len(invoice.get("lines") or [])
                example["first_lines"] = [
                    {field: (line.get(field)[:60] if field == "description" else line.get(field))
                     for field in LINE_FIELDS if line.get(field)}
                    for line in (invoice.get("lines") or [])[:EXAMPLE_LINES]]
                blocks.append(json.dumps(example, ensure_ascii=False)[:900])
        if not matched:
            return ""
        corrections = [entry for entry in self._corrections()
                       if entry.get("supplier_key") in matched or entry.get("supplier_key") == "unassigned"]
        out = ["", "Verified examples from this supplier's earlier invoices, confirmed by the operator. They show the "
                   "layout's conventions (which printed column is the description, where the barcode goes, how "
                   "quantities are written). Read the current document's own printed values; never copy these values."]
        out.extend("- " + block for block in blocks)
        if corrections:
            out.append("Operator-approved corrections for this supplier:")
            for entry in corrections[-MAX_CORRECTIONS:]:
                line = "- %s: %r was corrected to %r (%s)" % (
                    entry.get("invoice_line", ""), entry.get("original", ""), entry.get("correction", ""),
                    entry.get("reason", ""))
                out.append(line[:240])
        return "\n".join(out)

    @_locked
    def promote_feedback(self, entry, jobs):
        """Keep an owner-approved correction, attributed to the supplier of the invoice it names."""
        reference = _text(entry.get("Invoice / Line"))
        number = re.split(r"\s*/\s*|\s+line\s+", reference, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        normal = lambda value: "".join(char for char in _text(value).casefold() if char.isalnum())
        key = "unassigned"
        for job in jobs or []:
            invoice = (job or {}).get("invoice") or {}
            if number and normal(invoice.get("number")) == normal(number) and invoice.get("supplier_name"):
                key = supplier_key(invoice["supplier_name"])
                break
        record = {"feedback_id": entry.get("Feedback ID"), "supplier_key": key, "invoice_line": reference,
                  "original": _text(entry.get("Original Suggestion"))[:200],
                  "correction": _text(entry.get("User Correction"))[:200],
                  "reason": _text(entry.get("Reason"))[:200], "approved_on": _text(entry.get("Date"))}
        entries = [item for item in self._corrections() if item.get("feedback_id") != record["feedback_id"]]
        entries.append(record)
        self._save_corrections(entries)
        return record

    @_locked
    def retract_feedback(self, feedback_id):
        entries = self._corrections()
        kept = [item for item in entries if item.get("feedback_id") != feedback_id]
        if len(kept) != len(entries):
            self._save_corrections(kept)
        return len(entries) - len(kept)

    def summary(self):
        """Counts and statuses only; no invoice values."""
        corrections = self._corrections()
        suppliers = []
        for data in self._suppliers():
            template = data.get("template") or {}
            suppliers.append({
                "supplier_key": data["supplier_key"], "supplier_name": data.get("supplier_name"),
                "examples": len(data.get("samples", [])),
                "template": template.get("status", "none"), "confirmed": len(template.get("confirmed", [])),
                "fixes": template.get("fixes", []), "reason": template.get("reason"),
                "corrections": sum(1 for item in corrections if item.get("supplier_key") == data["supplier_key"]),
            })
        return {"suppliers": suppliers, "corrections": len(corrections),
                "unassigned_corrections": sum(1 for item in corrections if item.get("supplier_key") == "unassigned")}

    @_locked
    def forget(self, key):
        key = supplier_key(key)
        existed = self._supplier_path(key).exists() or self._template_path(key).exists()
        self._delete(self._template_path(key))
        self._delete(self._supplier_path(key))
        entries = self._corrections()
        kept = [item for item in entries if item.get("supplier_key") != key]
        if len(kept) != len(entries):
            self._save_corrections(kept)
            existed = True
        return existed

    @_locked
    def forget_sources(self, source_ids):
        """Drop everything learned from deleted invoices; relearn from what remains."""
        source_ids = {str(value) for value in source_ids}
        affected = []
        for data in list(self._suppliers()):
            key = data["supplier_key"]
            samples = [item for item in data.get("samples", []) if item["source_id"] not in source_ids]
            if len(samples) == len(data.get("samples", [])):
                continue
            affected.append(key)
            template = self._load_template(key)
            if template and (template.get("learned") or {}).get("source_id") in source_ids:
                self._delete(self._template_path(key))
                template = None
            if not samples:
                self._delete(self._supplier_path(key))
                self._delete(self._template_path(key))
                continue
            if template is None:
                outcome = {"status": "none", "reason": "source invoice deleted"}
                for sample in reversed(samples):
                    outcome = self._learn(key, sample, samples, None)
                    if outcome["status"] in ("learned", "relearned", "confirmed"):
                        break
                data["template"] = outcome
            data["samples"] = samples
            self._write(self._supplier_path(key), json.dumps(data, indent=1))
        return affected
