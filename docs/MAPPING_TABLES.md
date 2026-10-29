# Mapping workbook administration

Open when adapting the owner's workbook or operating mapping version history.
This document covers the header adapter, validation limits and additive storage.

Administrators open **Reference data & rules → Mapping tables**, upload an `.xlsx`,
review the report, then activate it. Validation never changes active rules. Any
flagged row prevents activation; fix it and upload again. VAT rows other than
VAT_TYPE C with PV in VAT_CODE are intentionally excluded and counted. All three
sheets must contain at least one usable row. Supplier rows without an id are errors.
The history records the activating administrator, UTC time, original filename and
per-sheet counts. Earlier versions can be restored and the active upload downloaded.
The local operator has the same administrative workflow.

## Adapting the 28 October workbook

`app/mapping_headers.py:SHEETS` is the single header adapter. For each fixed sheet,
`headers` maps the parser's canonical field to accepted Excel labels. Add the real
heading to that field's tuple. Case and surrounding whitespace are ignored;
internal whitespace and punctuation remain significant. `required` lists canonical
fields that must exist in row 1. Optional fields may be absent. Unknown columns are
ignored; aliases colliding on the same canonical field are rejected. The known
ENTITY AND CURENCY typo and corrected ENTITY AND CURRENCY already share one field.
No changes to storage, routes or UI are needed for a new header alias. If the sheet
names or business semantics change, review the parser and synthetic tests as well.

Examples: add `"Vat Percentage"` as a VAT_RATE alias or `"Site Currency"` as a
CURRENCY alias. Keep the original aliases for older workbooks. Update
`tests/test_mapping_tables.py` with a synthetic workbook covering the new alias;
never commit the owner's workbook or copy cell values into logs.

## Validation and limits

- `.xlsx` suffix plus OOXML content type; `.xls`, `.xlsm`, renamed macro workbooks,
  encrypted/broken ZIPs and duplicate ZIP entries are rejected.
- 8,000,000 bytes uploaded; 50,000,000 expanded bytes; at most 2,000 ZIP entries.
- At most 25,000 data-row positions and 64 columns per mapping sheet. Blank rows
  do not count in the report. Formula and Excel error cells are flagged. VAT dates
  must be Excel dates; rates must be finite and nonnegative.
- All three expected sheets are inspected even if another sheet is missing or has
  bad headings. Report issues identify only fixed sheet names, row numbers and
  fixed reasons/canonical field names. Unknown headings and cell values never enter
  reports or audit logs. Parser warnings/exceptions are sanitized at the API boundary.
- Up to 20 validated pending uploads; they expire after 24 hours and are purged on
  the next valid upload. Invalid uploads are not persisted. Uploaded workbook bytes
  are private administrative data and may be downloaded only by an administrator.

## Persistence and release

No environment variables or deployment changes. Startup adds one table using only
`CREATE TABLE IF NOT EXISTS mapping_table_versions`: id, state, created_at, metadata,
tables_json and workbook (all TEXT). Original workbook bytes use base64 TEXT for a
single schema on SQLite and PostgreSQL. Existing columns/tables are untouched.
`mapping_tables_active` is a new settings key. The existing `fine_rules_config` keeps
its format. PostgreSQL's existing advisory transaction lock / SQLite BEGIN IMMEDIATE
serializes activation and rollback. The workbook and parsed tables remain immutable
after activation; rollback changes only the active pointer/configuration and audit.

Only vat_codes, location_master, supplier_sites and the version label are replaced;
other rules settings are preserved. Before replacing an untracked configuration,
a snapshot is saved (including the initial configuration), so the first upload can
also be undone. Such snapshots cannot offer workbook download. If the older config
API changes these fields, the UI detects the discrepancy and stops claiming that
an earlier workbook represents the current configuration.

Rules signatures already include the config. Non-exported invoice results refresh
on their next read/export; exported invoices retain their saved result. The UI
reloads state after activation. Existing jobs and export receipts are not rewritten
in the activation transaction. Full history is retained without automatic pruning.

All five endpoints under `/api/mapping-tables` use the existing ADMIN authorization
registry, request protection and rate limiting. Members receive HTTP 403.
Release integration must synchronize `app/static` into the Hosting copies as usual;
this feature branch changes only source UI assets.
