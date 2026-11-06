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

## File cases, the full flow and AI faults

These need a throwaway PostgreSQL and Azurite of your own, named in `INV_STUDIO_DATABASE_URL`, `INV_STUDIO_BUCKET` and
`INV_STUDIO_AZURE_STORAGE_CONNECTION_STRING`. Never point them at a database anyone else uses.

    python -m tools.invoice_sim.run files --out /some/scratch/sim
    python -m tools.invoice_sim.flow --out /some/scratch/flow --port 36850
    python -m tools.invoice_sim.ai   --out /some/scratch/ai

- `files.py`: images, several invoices in one PDF, mixed and blank pages, locked, corrupt, oversized and non-invoice files, through the
  real upload route. Each must end in a clear message or a correct workbook, never a crash or an empty success.
- `flow.py`: a real server process on PostgreSQL. Upload, process, review with typed values, export, open the workbook, compare cells to
  the truth. Also several invoices at once, a kill mid-job, duplicates, re-export after lock. `--only blanks` runs the blank key cases.
- `ai.py`: the real app with the AI service replaced at the HTTP transport, so the provider code, parsing and every check still run. Any
  call to another host is recorded in `foreign.json`; it must stay empty. Cases: timeouts, 401, 403, 429, 500, invalid JSON, wrong
  totals, invented values, a wrong item pick and injected invoice text. Excel cells carry no mark, so "marked" means the app's review
  screen shows the AI mark for that cell. Each OCR engine really reads each file once and every case reuses that read, so a cell that
  changes was changed by the AI path, not by OCR timing out under load.

## How to read the numbers

- **WRONG no flag** is the number that matters: a value that differs from the truth with nothing telling the reviewer about that field.
- A flag is the field's own review cell, a reader gap, its evidence marked for review, or an issue this read raised that a perfect read
  of the same document does not. Issues that fire on every invoice do not count, or they would hide every silent wrong value.
- Every synthetic invoice stays at Review because no delivery location is printed, so the invoice-level status says nothing here.
- The item columns are scored as a reviewer who confirmed the printed supplier name would see them, from the exported Details sheet.
