# Item master and selected workbook

The October master has three independent adjacent blocks. The item block maps
`BARCODE`, `VPN`, supplier site and `ITEM_PARENT`. The PO block maps manual orders,
RMS orders, `BARCODE_1`, `RMS_ITEM_ID` and observed quantities. The location block
is a directory, without an order key. Physical row alignment establishes no join.

The dedicated administrator API accepts a standard `.xlsx` up to 16 MB, 150,000
rows, 17 exact headings (heading whitespace is trimmed) and 300 MB expanded XML.
It does not use or change the VAT/locations/supplier-sites mapping importer.

1. `POST /api/item-master/validate`, multipart `file`: reads the whole workbook,
   quarantines invalid literal/block keys and returns counts, bounded diagnostics,
   `upload_id` and `base_signature`. No live catalog changes. At most three pending
   uploads are retained; delete an unwanted pending upload with
   `DELETE /api/item-master/uploads/{upload_id}`.
2. `POST /api/item-master/uploads/{upload_id}/activate`: refuses a changed base
   with 409. One database transaction replaces the item catalog and sparse PO block,
   retaining richer existing PO/GRN source records. Existing mapping configuration
   is untouched. Every version retains its complete activated composition privately.
3. `GET /api/item-master`: returns current signature, active version and history.
4. `POST /api/item-master/versions/{id}/rollback?expected_signature={current}`:
   restores the exact version, including original legacy semantic mappings and
   corroborating receipt sources. A stale expected signature returns 409.

Source references qualify the original physical sheet and row by source hash and
logical block: `sha256:kind:sheet!row`. A bare physical reference that could name
more than one block cannot verify a cell. Original column names remain in source
records; numeric Excel identifiers with explicit zero masks retain those zeros.
Numeric identifiers at or beyond 15 digits are quarantined because Excel may have
lost precision. Literal text identifiers retain all digits. Formula/error/date
cells are reported and quarantined, never evaluated.

Invoice matching resolves the issuer/site or approved family before product
search. It uses VPN, then barcode, then manual description candidates. Every exact
identifier retains its own candidate set; contradictory identifiers hold. Unknown
variant attributes remain compatible. Repeated master parents do not create more
invoice lines. Printed pair/pack tokens never multiply invoice quantities.

Order association joins PO `BARCODE_1` to item `BARCODE` and also requires
`RMS_ITEM_ID == ITEM_PARENT` within the resolved supplier site. Printed RMS/manual
orders and evidenced item quantities can restrict candidates. Multiple candidates
hold. A destination requires a unique delivery-region directory match or unique
corroborating configured PO/GRN destination; telephone/bill-to substrings do not
establish delivery evidence. Warehouse evidence overrides the Store default, and
contradictory configured types hold. VAT, country, currency, active dates and site
status continue to use approved configuration. Invoice quantities, costs, date,
net and VAT remain invoice values.

Sparse master quantities have no receipt identity. They never establish available
balances and final export remains held even after generic review confirmation.
A richer configured PO source may establish capacity only with explicit
`RECEIPT_ID`, order, location, supplier code, item and quantity. Duplicate receipt
identity/item rows do not add capacity; contradictory quantities hold. Stable
supplier-family/receipt/item keys are reserved in the existing export ledger.
Reviewed final quantities and header scope are checked against those capacities.
Selected invoices share the locked ledger, so conflicts refuse the whole batch.

Final workbooks retain the exact Header/Tax_Breakdown/Details headers, text-safe
identifiers, dates, empty references and default empty UPC. Costs and quantities
use four decimal display places. Selected order determines 1..N transaction keys.
Rules/catalog signatures and evidence hashes are checked again under the export
transaction; all workbook and receipt writes roll back together on failure.

Validation and activation are synchronous bounded-file operations; no new wall
clock deadline or automatic retry is claimed. SQLite lock acquisition waits up to
30 seconds; cloud connection establishment uses its existing 20-second timeout.
Activation may hold the catalog mutation lock while building indexes. On a process,
connection or write failure the database transaction keeps the prior catalog.
Retry validation after a transient read failure; refresh before retrying a 409.
The existing PostgreSQL shadow importer retains its two-second cutover lock bound.
No cloud deployment is part of this change.
