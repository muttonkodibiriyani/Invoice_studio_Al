# The hard-case corpus: synthetic invoices that are hard to read, with checked ground truth

`hard-cases/` holds 20 cases for `scripts/engine_scorecard.py`: 12 printed PDFs and 8 scans of them. Each case is a
document and its expected JSON under the same stem, the flat layout of `samples/`:

```bash
.venv/bin/python scripts/engine_scorecard.py hard-cases --out /tmp/scorecard-hard
```

Everything is made up: suppliers, buyers, codes, barcodes, amounts. So the corpus is committed. Real supplier
invoices never go into git, an image or a cloud provider. They stay in a local corpus directory and only their
scores leave the machine. `hard-cases/` is outside `samples/`, so the Docker image (`COPY samples`) does not carry it
and `engine_scorecard.py samples` does not see it.

## Adding or changing a case

Edit `CASES` (a printed invoice) or `SCANS` (a printed case scanned) in `scripts/build_hard_corpus.py`, then run
`.venv/bin/python scripts/build_hard_corpus.py`. It rewrites `hard-cases/` whole. The document and its JSON come from
one spec, so what the page prints and what a reader is expected to return cannot drift apart. Never hand-edit a
JSON in `hard-cases/`: `tests/test_hard_corpus.py` fails on it.

## Ground-truth rules

The expected JSON holds what the invoice prints, in the `samples/*.json` shape plus `net_amount` per line:
`number, supplier_name, date, date_printed, po, currency, net, tax,
lines[sku, gtin, description, uom, qty, price, net_amount]`.

- `supplier_name` is the issuing company, never the bill-to party.
- `date_printed` is the date exactly as printed. `date` is the same date in ISO 8601, with one exception: when
  both leading parts of a numeric date are 12 or less and differ (`05/10/2026`), `date` is null. The app keeps such a
  date only in `date_printed` and does not guess a locale (`app/fine_rules.py` `parse_printed_date`, V-008), so a
  reader that guesses either way is wrong. Any other numeric date is day-first: `19/10/2026` is 19 October.
- `net` is the taxable amount before VAT, after any invoice discount. `tax` is the VAT amount; with no VAT line
  printed it is null, never 0.
- Amounts are plain numbers at the decimals the invoice prints, with no thousands separators.
- `description` is the whole item text; a cell wrapped over several rows is joined with single spaces.
- `gtin` is a printed barcode only when its GS1 check digit is right (the app's own rule): one that fails is null.
- A field or column the invoice does not print is null, never `""`.

## How the ground truth is checked

`tests/test_hard_corpus.py` checks the corpus against the printed pages, never against a reader:

| Test | Catches |
|---|---|
| every expected value is printed on the page | an expected value the PDF's text layer (pdfplumber) does not hold |
| the committed header is printed beside its label | a value wired to the wrong key (the PO number in `number`), the buyer as supplier |
| the expected date is the printed date, and an ambiguous one is not guessed | a guessed `date` for `05/10/2026` (either way), a month-first date, a `date_printed` that is not printed beside its label |
| the sums hold | a line amount that is not qty × price, lines (less the discount) not adding up to `net`, VAT off by a cent |
| a GTIN is expected only when its check digit is right | a bad barcode expected as a GTIN, a good one dropped |
| the committed JSON is what the spec writes | a hand edit, or a spec change not regenerated |
| the scorecard accepts the expected JSON | a case the scorecard would refuse as `bad_expected` |
| a scanned PDF has no text layer; page two of h05 | a "scan" a text reader could read, a two-page case that is not two pages |

The first draft expected h01, h04 and h10's dates read day-first. The first scorecard run showed the reader leaving
`date` empty and filling `date_printed`, and the app turned out to do that on purpose (V-008), so the rule above
replaced the draft. That is the only change the scorecard caused: a reader's output is never copied into the
expected JSON.

Before this suite was trusted, four wrong ground truths were planted, and the suite failed on each: net one cent
off, h04's date given month-first, `supplier_name` taken from the buyer, and `po` taken from `number`.

By hand (31 Oct 2026): every printed page and the s02 and s04 scans were looked at against their JSON, and every
line, subtotal, discount and VAT was recomputed. Two VAT values sit on a half-cent: 105.125 is printed as 105.13
(h06) and 121.875 as 121.88 (h08), both rounded half-up.

## The cases

| Case | What makes it hard |
|---|---|
| h01-thousands-separators | every amount over 999 prints a thousands separator; a 5-digit net |
| h02-kwd-three-decimals | Kuwaiti dinars, three decimals (fils) on every amount; no VAT line, so `tax` is null |
| h03-month-name-date-lpo | 'Tax Invoice No.', 'Date of Supply', 'LPO Ref' labels; date `04-Oct-2026` |
| h04-day-first-date | `05/10/2026`: 5 October or 10 May. A reader must not guess, so `date` is null and `date_printed` holds it (h01 `12/10` and h10 `08/10` are ambiguous too; the other numeric dates are not) |
| h05-two-page-table | 36 rows over two pages, the header repeated on page 2, the totals on page 2 |
| h06-wrapped-descriptions | item texts wrapped over two rows in a narrow description column |
| h07-reordered-columns | Qty and Unit first, the code after the text, the barcode last; header fields in a right-hand box; an invoice number with a space (`NWT 004512`) |
| h08-discount-before-vat | a discount between the subtotal and the taxable amount: the rows add up to the subtotal, not to `net` |
| h09-decimal-qty-and-price | kilogram quantities with decimals; unit prices with three decimals in AED; an empty barcode column |
| h10-bad-barcode | one printed barcode fails its check digit, so that line's `gtin` is null |
| h11-description-only | no code, barcode or unit column, and no PO; lines are matched by description |
| h12-buyer-printed-first | the bill-to party printed first and largest; the supplier only in a 'From' block and the footer |
| s01-scan-skewed-thousands | h01 at 150 dpi, skewed 1.6°, with noise |
| s02-scan-jpeg-reordered | h07 skewed -2.4° and saved as JPEG quality 35 |
| s03-image-only-pdf | h03 as a scanned PDF with no text layer |
| s04-faded-photocopy | h09 as a faded, low-contrast grey photocopy (JPEG) |
| s05-two-page-scan-pdf | h05's two pages as one image-only PDF |
| s06-tiff-day-first | h04 as a greyscale TIFF |
| s07-kwd-scan | h02 scanned: three-decimal amounts through OCR |
| s08-discount-scan | h08 scanned |

## Not covered yet

- Arabic or bilingual invoices. The venv has no Arabic shaping library, and badly shaped glyphs would be a made-up
  difficulty, not a real one.
- Photographs (perspective, shadows, creases), handwriting, stamps over figures, credit notes, several invoices in
  one file.
- Real layouts. These cases are hard in ways chosen in advance. The baseline on real invoices will find the ways
  nobody chose, and each fixed cluster should come back here as a synthetic case.
