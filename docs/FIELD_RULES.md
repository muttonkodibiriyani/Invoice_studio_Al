# Field rules for the local readers

One rule per scored field. The readers, the hard-case truth (`scripts/build_hard_corpus.py`), real-corpus truth and
the scorecard (`scripts/engine_scorecard.py`) all aim at these answers. All examples are made up.

**General**
- A value is what the invoice prints, normalised as below. Nothing is guessed, inferred from the supplier, computed
  from other fields, or taken from the file name.
- Not printed, or unreadable → `null`. Never `""`, never `0` in place of a missing amount.
- Strings: trim, collapse whitespace runs to one space, keep the printed case and punctuation. (The scorecard
  case-folds, so case never decides a score.)

## Numbers (net, tax, qty, price, net_amount, tax_amount)

- Plain decimal string: `.` as the point, no thousands separator, printed places kept (`9.00` stays `9.00`, KWD
  `12.500` stays `12.500`), `-` for a negative. Implemented in `app/numbers.py`.
- Accepted: `1,067.13`, `1,234,567`, `8 800.00`, `1.067,13`; a currency code or sign before or after, with or
  without a space (AED USD EUR GBP KWD SAR QAR BHD OMR INR Dh Dhs KD SR QR BD RO); `-12.50`, `(12.50)`, `12.50-`,
  Unicode minus; OCR noise: `O`/`o` among digits as 0, spaces around a separator (`1 , 105.31`), Arabic-Indic digits
  and the Arabic decimal and thousands marks.
- Separators: one comma + 3 digits groups thousands (`1,067` = 1067); one comma + 1-2 digits is the point
  (`950,40`); one point is always the point (`1.067` = 1.067); one kind printed twice or more: the last is the point
  unless its group has 3 digits (`2.480.00` = 2480.00, `1.234.567` = 1234567).
- Not a number → `null`: a percent, two numbers in one cell (`12 3.50`), letters, a lone `O`, a comma + 4 or more
  digits, more than 24 digits.
- A unit glued to qty is split off (`9.00 Units` → qty `9.00`, uom from `Units`).
- A line or total amount the layout reader reads is never negative: a bracketed figure there is a discount or
  credit, not the field.
- A printed `0.00` is a value: `0.00`, not `null`.

## Dates (date, date_printed)

The invoice date only: labels `Invoice Date`, `Date`, `Tax Invoice Date`, `Document Date`. Never `Due Date`,
`Delivery Date`, `LPO Date`, `Order Date`.

- `date_printed`: exactly as printed (`8/13/2026`, `04-Oct-2026`), whitespace collapsed. Null only if no invoice date.
- `date`: ISO `YYYY-MM-DD`, decided **from that one printed date alone**:
  - month name (`04 Oct 2026`, `04-Oct-2026`, `Oct 4, 2026`) → that date;
  - year first (`2026/10/04`, `2026-10-04`) → that date;
  - numeric, year last, one leading part > 12 → that part is the day (`19/10/2026` → `2026-10-19`,
    `8/13/2026` → `2026-08-13`);
  - both leading parts equal (`09/09/2026`) → that date;
  - both ≤ 12 and different (`05/10/2026`) → **`null`** (date_printed still filled). This is the app's V-008 rule.
    Do not use the supplier's other invoices to decide: one supplier prints month-first on some invoices and
    day-first on others. Do not use other dates on the page either.
  - two-digit year → `20yy`.

## Header fields

**number**: the supplier's invoice number as printed, prefixes and separators kept (`SI/2026/0101`, `INV-0042`).
Labels, with or without a colon, label beside or above the value: `Invoice No`, `Invoice No.`, `Invoice #`,
`Invoice Number`, `Tax Invoice No`, `Tax Invoice Number`, `Doc No`, `Document No`, `Bill No`. A title line
`Tax Invoice <value>` also counts. Never the PO, a delivery note, a sales order (`Source`, `SO`) or the file name.

**supplier_name**: the company issuing the invoice: letterhead, top block, or footer. Take the fullest printed name,
including the legal form (`Example Trading LLC`, `Sample FZ-LLC`) when it is printed anywhere; otherwise the printed
trade name. Never the `Bill To` / `Ship To` / `Customer` party, an address line, a logo tagline or a stamp.

**po**: the buyer's purchase order number. Labels: `PO`, `PO No`, `PO Number`, `P.O. #`, `Purchase Order`,
`Customer PO`, `LPO`, `LPO No`, `LPO Ref`, `Order Ref`. Anywhere on the invoice: header, a header table cell under
its heading, or a line under the item table on the last page. A leading document-type word joined to the number is
dropped: `LPO-77310`, `PO 77310`, `PO#77310` → `77310`. Any other prefix is kept. Never `Source`, `SO`,
`Sales Order`, `Delivery Note`, `Quotation`.

**currency**: ISO 4217 code. From a `Currency` cell, a code beside amounts or totals (`Total (AED)`, `950.40 AED`),
or the amount in words (`Dirham` → AED). Symbols: `Dhs`/`Dh`/`د.إ` → AED, `KD` → KWD, `SR` → SAR, `QR` → QAR,
`BD` → BHD, `RO` → OMR. Not from the supplier's country.

**net**: taxable amount before VAT, after any invoice-level discount. Labels: `Untaxed Amount`,
`Total Untaxed Amount`, `Total Excl. VAT`, `Taxable Amount`, `Net Amount`, `Subtotal` (only when no discount is
taken after it). Discount after subtotal → net is the post-discount taxable figure, not the subtotal.

**tax**: total VAT. Labels: `VAT`, `VAT 5%`, `Total VAT`, `Tax`. Printed `0.00` → `0.00`. No VAT line printed → `null`.

## Line building

- One line per item row: a row that prints its own qty, price or amount. Lines are in printed order.
- **Continuation rows**: text rows under an item row, up to the next item row, that print no qty/price/amount,
  belong to that line's description (joined with one space, in printed order, even when they repeat the name).
- **Two-page tables**: the table continues on the next page. Repeated column headings, page headers and footers,
  and `carried forward` / page subtotals are not lines.
- **Not lines**: subtotal, total, VAT, discount, total-quantity rows (a lone `87` under the qty column).
- **Zero-priced lines are lines**: gift (GWP), tester, sample lines, 100% discount lines, `0.00` amounts. Keep them
  with their printed values.

## Line fields

**sku**: the supplier's item code, as printed, letter suffixes kept. From a column headed `Item Code`, `Code`,
`SKU`, `Article`, `Item No`, `Product Code`, `Ref`; a labelled `SKU: X` in the description; or a bracketed code at
the start of the description (`[3001234567897] Body lotion` → `3001234567897`; `[300123456789T] Tester` →
`300123456789T`). Never a serial number or a `Barcode` column.

**gtin**: a GS1 barcode, digits only, at its printed length (8, 12, 13 or 14 digits, no padding), only when the
check digit is right; otherwise `null` (the reader keeps it in `barcode_unchecked`). From a `Barcode`, `EAN`,
`UPC`, `GTIN` column, or a bracketed description code that is all digits and passes the check (then it fills both
sku and gtin). A code with a letter (`…T` = tester) is not a GTIN: gtin `null`. Never strip the letter to make one.

**description**: the item text, with the leading code or bracket removed and labelled metadata (`SKU:`, `COO:`,
`HS code:`) removed; wrapped cells and continuation rows joined with one space. A `Size` / `Volume` column is not
appended.

**qty**: the `Qty` / `Quantity` column number (see Numbers). A unit word glued to it goes to uom.

**uom**: the unit, mapped onto the vocabulary below. From a `Unit`, `UOM`, `U.O.M`, `Unit of Measure` column, a unit
word glued to qty, or a heading `Qty in CTN`. A `Size`, `Pack Size` or `Volume` column (`50ml`) is **not** a unit of
measure. No unit printed → `null`.

**price**: the unit price before line discount and before VAT: `Unit Price`, `Rate`, `Price`, `Unit Price Excl.
VAT`. With a `Disc.%` column, price is the printed list price (`192.00`), not the discounted one. A gift line with
a 100% discount keeps its printed price (`0.98`).

**net_amount**: the printed line amount after line discount, before VAT: `Amount`, `Value Exc. Tax`, `Net Amount`,
`Taxable Amount`, `Amount Excl. VAT`. Never a `Total` / `Total Amount` column that includes VAT when an excl. column
is printed. Only an incl.-VAT amount printed → `null` (never computed).

## Unit-of-measure vocabulary

Map spelling, not meaning: `EA` and `PCS` stay different codes, because suppliers print them as different units.
Matching is case-insensitive, with a trailing `.` and plural `s` ignored.

| uom | supplier spellings |
|---|---|
| PCS | PCS, PC, Piece, Pieces, Unit, Units, Nos, No |
| EA | EA, Each |
| BOX | BOX, BX, Boxes |
| CTN | CTN, Carton, Cartons, CS, Case |
| SET | SET, Sets |
| PKT | PKT, Packet, Pack, PK |
| BTL | BTL, Bottle |
| CAN | CAN, Tin |
| BAG | BAG |
| ROL | ROL, Roll |
| KG | KG, Kgs, Kilo, Kilogram |
| G | G, GM, Gram |
| L | L, LTR, Litre, Liter |
| ML | ML |
| M | M, MTR, Metre, Meter |
| DZ | DZ, DZN, Dozen |
| PR | PR, Pair |

A unit not in the table: write it upper-cased as printed, and propose it here.

## Where the truth and the scorecard stand

- Scorecard comparison rules agree with these rules (amounts by value within 0.0005, `date` normalised, everything
  else a trimmed case-folded string, so uom must be the vocabulary code in truth: `Units` truth is `PCS`).
- **Flagged truth**: hard cases h03 and s03 expect po `LPO-77310`; under the po rule it is `77310`. To be changed in
  `build_hard_corpus.py` by the header-field task.
- `docs/HARD_CORPUS.md` "any other numeric date is day-first" is replaced by the date rule above (the part over 12
  is the day, so `8/13/2026` is 13 August).
