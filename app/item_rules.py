"""The item master rules workbook (W.ENGINE): the matcher's rules are its rows, so a new edge case is a new row.

The workbook is versioned and pinned by sha256: ``<workbook>.sha256`` next to it, or INV_STUDIO_RULES_SHA256.
INV_STUDIO_RULES_WORKBOOK points at another copy (Azure mounts it). A workbook that is changed, unpinned or
inconsistent is refused whole; nothing falls back to built-in rules.
"""
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re

import openpyxl

DEFAULT_WORKBOOK = Path(__file__).resolve().parents[1] / "docs" / "rules" / "Invoice_ItemMaster_Rules_v0.2.xlsx"
_LIMIT = re.compile(r"\b(?:top|max)\s+(\d+)", re.I)


class RulesError(ValueError):
    """The rules workbook cannot be used as it is."""


@dataclass(frozen=True)
class LookupRule:
    id: str
    step: int
    step_name: str
    order: int
    rule: str
    columns: tuple   # item master columns, searched in this order ("BARCODE (then BARCODE_1)"); () = none
    match: str       # exact | number | fuzzy; "" when the row is not a column search (a read, a check, a lookup)
    confirm: bool    # a hit is a suggestion the reviewer confirms, never a filled cell
    limit: int | None  # most candidates shown ("top 3", "max 5")
    on_one: str
    on_none: str
    on_many: str
    prompts: tuple

    @property
    def terminal(self):
        return not self.columns


@dataclass(frozen=True)
class CoverageTier:
    name: str
    low: Decimal            # inclusive, percent
    high: Decimal | None    # exclusive; None = and above
    action: str
    export: str
    prompt: str | None


@dataclass(frozen=True)
class Prompt:
    id: str
    when: str
    message: str   # str.format template, e.g. "Line {n}: ..."
    buttons: tuple


def _text(value):
    return "" if value is None else str(value).strip()


def _rows(wb, sheet, *required):
    if sheet not in wb.sheetnames:
        raise RulesError(f"Rules workbook has no {sheet} sheet")
    rows = wb[sheet].iter_rows(values_only=True)
    header = [_text(h) for h in next(rows, ())]
    missing = [h for h in required if h not in header]
    if missing:
        raise RulesError(f"{sheet} lacks column(s) {', '.join(missing)}")
    at = {h: header.index(h) for h in required}
    for row in rows:
        values = {h: row[i] if i < len(row) else None for h, i in at.items()}
        if any(_text(v) for v in values.values()):
            yield values


def _prompt_ids(value):
    return tuple(p for p in (_text(x) for x in _text(value).split("/")) if p and p != "-")


def _lookup_rule(row):
    rule_id, step = _text(row["Rule ID"]), _text(row["Step"])
    number = re.match(r"\d+", step)
    if not rule_id or not number:
        raise RulesError(f"Lookup_Sequence row {rule_id or '?'} needs a Rule ID and a numbered Step")
    searched = _text(row["Item master column searched"])
    primary = re.split(r"\s*\(", searched)[0].strip()
    columns = () if primary in ("", "-") else (primary, *re.findall(r"\(then\s+([^)]+)\)", searched))
    kind = _text(row["Match type"]).lower()
    match = "fuzzy" if "fuzzy" in kind else "number" if kind.startswith("exact number") else (
        "exact" if kind.startswith("exact") else "")
    limit = _LIMIT.search(f"{row['Match type']} {row['If more than one match']}")
    return LookupRule(rule_id, int(number.group()), step, int(row["Order in step"] or 0), _text(row["Rule"]),
                      columns, match,
                      _text(row["If exactly one match"]).lower().startswith("suggestion"),
                      int(limit.group(1)) if limit else None, _text(row["If exactly one match"]),
                      _text(row["If no match"]), _text(row["If more than one match"]),
                      _prompt_ids(row["Reviewer sees (short)"]))


class RuleSet:
    """Typed, read-only view of one checksummed rules workbook."""

    def __init__(self, path, sha256, version, lookup, variants, tiers, taxes, prompts):
        self.path, self.sha256, self.version = path, sha256, version
        self._lookup, self._variants, self._tiers, self._taxes, self._prompts = lookup, variants, tiers, taxes, prompts

    def lookup_order(self, step=None):
        """Enabled Lookup_Sequence rules by step then order; ``step`` (e.g. 2) keeps one step."""
        return tuple(r for r in self._lookup if step is None or r.step == step)

    def location_type_variants(self):
        """Item master value as written in the workbook -> written form; '(empty)' is ''. Anything else is held."""
        return dict(self._variants)

    def coverage_tiers(self):
        """Coverage tiers, highest first."""
        return self._tiers

    def tax_code(self, country):
        """Tax code for a country or market name (any case), else None."""
        return self._taxes.get(_text(country).casefold())

    def prompts(self):
        """Prompt id -> Prompt."""
        return dict(self._prompts)


def _sha256(path, expected):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pin = path.with_name(path.name + ".sha256")
    expected = _text(expected) or (pin.read_text().split()[0] if pin.is_file() and pin.read_text().split() else "")
    if not expected:
        raise RulesError(f"Rules workbook {path.name} has no checksum ({pin.name} or INV_STUDIO_RULES_SHA256)")
    if digest != expected.lower():
        raise RulesError(f"Rules workbook {path.name} does not match its checksum")
    return digest


def load(path=DEFAULT_WORKBOOK, sha256=None):
    """Read and check one rules workbook; raises RulesError rather than return a partial rule set."""
    path = Path(path)
    if not path.is_file():
        raise RulesError(f"Rules workbook {path} not found")
    digest = _sha256(path, sha256)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        version = next((_text(r[1]).split(",")[0] for r in wb["README"].iter_rows(values_only=True)
                        if len(r) > 1 and _text(r[0]) == "Version"), "") if "README" in wb.sheetnames else ""
        lookup = []
        for row in _rows(wb, "Lookup_Sequence", "Rule ID", "Step", "Order in step", "Rule",
                         "Item master column searched", "Match type", "If exactly one match", "If no match",
                         "If more than one match", "Reviewer sees (short)", "Enabled"):
            entry = _lookup_rule(row)
            if any(r.id == entry.id for r, _ in lookup):
                raise RulesError(f"Rule {entry.id} appears more than once in Lookup_Sequence")
            lookup.append((entry, _text(row["Enabled"]).upper() == "Y"))
        lookup = sorted((r for r, on in lookup if on), key=lambda r: (r.step, r.order))
        variants = {}
        for row in _rows(wb, "Location_Type_Variants", "Item master value", "Written as"):
            value, written = _text(row["Item master value"]), _text(row["Written as"])
            if value.startswith("(") and value != "(empty)":
                continue
            variants["" if value == "(empty)" else value] = written
        tiers = []
        for row in _rows(wb, "Coverage_Tiers", "Tier", "From % (inclusive)", "To % (exclusive)", "What happens",
                         "Export", "Reviewer sees"):
            if row["From % (inclusive)"] is None:
                continue  # a note row
            high = row["To % (exclusive)"]
            tiers.append(CoverageTier(_text(row["Tier"]), Decimal(str(row["From % (inclusive)"])),
                                      None if high is None else Decimal(str(high)), _text(row["What happens"]),
                                      _text(row["Export"]), (_prompt_ids(row["Reviewer sees"]) or (None,))[0]))
        tiers = tuple(sorted(tiers, key=lambda t: t.low, reverse=True))
        for upper, lower in zip(tiers, tiers[1:]):
            if lower.high != upper.low:
                raise RulesError(f"Coverage tier {lower.name} ends at {lower.high}, not at {upper.low}")
        if tiers and (tiers[0].high is not None or tiers[-1].low != 0):
            raise RulesError("Coverage tiers must run from 0 with an open top tier")
        taxes = {}
        for row in _rows(wb, "Tax_Codes", "Country", "Market", "Tax Code"):
            code = _text(row["Tax Code"])
            if not code:
                raise RulesError(f"Tax_Codes row {_text(row['Country'])} has no Tax Code")
            for name in (row["Country"], row["Market"]):
                if _text(name):
                    taxes[_text(name).casefold()] = code
        prompts = {}
        for row in _rows(wb, "Reviewer_Prompts", "Prompt ID", "When", "Message (keep it short)", "Buttons"):
            pid = _text(row["Prompt ID"])
            prompts[pid] = Prompt(pid, _text(row["When"]), _text(row["Message (keep it short)"]),
                                  tuple(b.strip() for b in _text(row["Buttons"]).split("·") if b.strip()))
    finally:
        wb.close()
    named = {p for r in lookup for p in r.prompts} | {t.prompt for t in tiers if t.prompt}
    unknown = sorted(named - set(prompts))
    if unknown:
        raise RulesError(f"Rules name prompt(s) missing from Reviewer_Prompts: {', '.join(unknown)}")
    return RuleSet(path, digest, version, tuple(lookup), variants, tiers, taxes, prompts)


@lru_cache(maxsize=1)
def rules():
    """The deployed rule set: INV_STUDIO_RULES_WORKBOOK (+ INV_STUDIO_RULES_SHA256), else the shipped workbook."""
    return load(os.getenv("INV_STUDIO_RULES_WORKBOOK") or DEFAULT_WORKBOOK, os.getenv("INV_STUDIO_RULES_SHA256"))
