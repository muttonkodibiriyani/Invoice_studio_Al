"""Header Location and Location Type from the matched item master row (rules L4.1 and L4.2).

Location Type is written 'Store (S)' or 'Warehouse (W)', the target template's text (fine_rules.STORE);
the workbook's 'Store(S)' / 'Warehouse(W)' are the same values. The item master's spellings are rows
in the rules workbook's Location_Type_Variants sheet, compared in any case and with or without spaces;
a value that is not a row is held for the user to pick (P-LOCTYPE). Location is the row's LOCATION
NUMBER; when it is blank the user adds it (P-LOC)."""
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping

from .fine_rules import STORE, WAREHOUSE

TYPES = (STORE, WAREHOUSE)
ASK_LOCATION = "P-LOC"
ASK_LOCATION_TYPE = "P-LOCTYPE"


def _key(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def written(value):
    """The written form of a Location Type already set as Store (S) / Warehouse (W), spaced or not; else None."""
    return next((t for t in TYPES if _key(t) == _key(value)), None)


def normalise(value, variants: Mapping[str, str]):
    """Store (S) or Warehouse (W) for an item master value, or None when it is held for the user.

    variants is RuleSet.location_type_variants(): workbook value (blank cell as '') to written form."""
    table = {_key(raw): written(to) for raw, to in variants.items() if written(to)}
    return table.get(_key(value))


@dataclass(frozen=True)
class Location:
    id: str | None
    type: str | None
    prompts: tuple[str, ...]


def resolve(row: Mapping, variants: Mapping[str, str]) -> Location:
    """Location and Location Type from the matched item master row, with the prompts the user must answer."""
    number = str(row.get("LOCATION NUMBER") or "").strip() or None
    names = [row.get(c) for c in ("LOCATION NAME", "LOCATION NAME 2") if _key(row.get(c))]
    kind = next(filter(None, (normalise(n, variants) for n in names or [None])), None)
    prompts = (ASK_LOCATION,) * (number is None) + (ASK_LOCATION_TYPE,) * (kind is None)
    return Location(number, kind, prompts)


def prompts(ids, rule_prompts: Mapping, value=None) -> list[dict]:
    """The workbook's prompts for ids (RuleSet.prompts()), buttons in the written form, for the rules view."""
    return [{"id": p.id, "message": p.message.format_map(defaultdict(str, value=value or "")),
             "buttons": [written(b) or b for b in p.buttons]}
            for p in (rule_prompts.get(i) for i in ids) if p]
