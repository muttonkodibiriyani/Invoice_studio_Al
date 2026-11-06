# Invoice simulation

Made-up invoices with a known truth, read by the real readers with the AI off, scored field by field. Everything here is
synthetic: **the results do not measure the real invoices.** No private invoice value belongs in this tool, its output or its reports.

## What it does

- `tools/invoice_sim/generator.py`: 49 layout families of 5 invoices each, fixed seed. The families and label wordings were written
  before reading the reader's label lists (commit db0b72a), so a family the reader does not know stays unknown.
- `scan.py`: the same PDFs scanned at low DPI with skew and noise (`office`, `fax`, `phone`, `bad-copy`).
- `degrade.py`: carbon copies, photos and poor scans, stamps and overlaps, as raster renders.
- `master.py`: an Item Master and rules config in the app's own upload format, holding codes and barcodes in the master, not in it,
  ambiguous, and belonging to another supplier.
- `score.py`: correct, blank or WRONG per field and per line.
- `run.py`: the full run, by hand. Reads are saved per document, so a stopped run resumes.

## Run it

Point everything at a disk with room. A full run takes hours on a shared host.

    export TMPDIR=/some/scratch/tmp INVOICE_SIM_HANDWRITING_FONT=/path/to/Kalam-Regular.ttf
    python -m tools.invoice_sim.run build --out /some/scratch/sim
    python -m tools.invoice_sim.run read  --out /some/scratch/sim --only carbon-,ink- --workers 4
    python -m tools.invoice_sim.run score --out /some/scratch/sim

`--only` takes document id prefixes and `--variant` takes `native`, `scan`, `asis` or `repo`. Without a handwriting font, handwriting
falls back to a jittered oblique font. Either way a real pen is harder: read those counts as an upper bound.

`tests/test_invoice_sim.py` is the part CI runs: no OCR models, with the real generator, text-layer read, rule chain and workbook.

## How to read the numbers

- **WRONG no flag** is the number that matters: a value that differs from the truth with nothing telling the reviewer about that field.
- A flag is the field's own review cell, a reader gap, its evidence marked for review, or an issue this read raised that a perfect read
  of the same document does not. Issues that fire on every invoice do not count, or they would hide every silent wrong value.
- Every synthetic invoice stays at Review because no delivery location is printed, so the invoice-level status says nothing here.
- The item columns are scored as a reviewer who confirmed the printed supplier name would see them, from the exported Details sheet.
