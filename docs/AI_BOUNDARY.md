# When Invoice Studio calls the AI

The local readers (invoice2data, PaddleOCR, Docling) read every invoice first. The AI is a boundary after them,
not a reader beside them. The code is `process()` in `app/engines.py` (the comment above `AI_REASONS`), and the
tests are the AI-boundary tests in `tests/test_reader_chain.py`.

## What the local result asks of the AI (`readers.ai.need`)

| need | When | What the AI may change |
|---|---|---|
| `fallback` | The local result fails `_basic_checks`: no lines, no number or net, or lines that do not sum to the net. | It fills empty fields and flags every disagreement. Its lines stand in only for local lines that do not sum to a printed net (`merge_ai_fields`). On a **weak scan read** see below. |
| `gap_fill` | The local result passes the checks but leaves gaps (a missing date, an item code, a unit). | It fills empty fields only. Every value a local reader read stays, and a different AI value is flagged for review. |
| `cross_check` | The local result is complete and reconciled. | Nothing, unless the owner opts in with `ai_cross_check` or `INV_STUDIO_AI_CROSS_CHECK=1`. Then it flags disagreements and fills optional empty fields. |

`need` is recorded on every job, whether or not the AI ran, so AI calls per invoice can be counted on an AI-off run.

**AI fallback off** (`ai_fallback=false`): no `need` calls the AI, for any engine. The job says `AI fallback is off`.

## The two paths where the AI is more than a gap filler

1. **`engine="ai"`: the user chose the AI as the reader.** That is a choice, not a fallback, so it runs whatever
   `ai_fallback` says. The preflight warns that the document goes to the provider. The AI's read is kept only
   when it reads strictly more than a local pass (office documents and the local Claude bridge read locally
   first). The reason is "The AI was the selected reader and read the whole invoice" or, when a local read wins,
   "…a local reader read as much, so the local read is kept".
2. **No local reader read anything.** In `fallback`, the AI's read becomes the whole invoice, and every field names
   `ai`. On a PDF the reason is "No local reader read this invoice, so the AI read the whole invoice". On a scan it
   goes through the weak-scan rule below, which says "replaced the whole invoice".

## The weak-scan rule

It runs only in `fallback`, only on a scan, and only when `scan_guard.weak_read` finds the local read weak. A
local read that passes the basic checks is never replaced, even when it is weak (for example, no item codes): it
goes through `gap_fill`.

- If the AI's read of the page image fails its own checks (`scan_guard.ai_self_check`), nothing is filled and the
  job is `not_read`.
- Otherwise the AI replaces **only the fields that are empty or disagree**. A net or tax total needs the AI's quote.
  Local lines that align one to one with the AI's lines (same count, `_aligned` pairwise) are kept, and only
  their empty or differing figures change. Only when the lines do not align do the AI's lines replace them all.
- Example (diagnosis C2): PaddleOCR reads the net as 8.0 from `Net total: 8 800.00`. The AI replaces the net and
  nothing else. The lines keep `paddleocr` as their reader, and the reason ends "replaced only the fields that are
  empty or disagree: net".

## What every job records

- `readers.header[field]` and `readers.lines[n][field]` name the reader of each value: the engine id
  (`invoice2data`, `paddleocr`, `docling`, `native_pdf_text`) or `ai`. A field filled from another local
  reader's read names that reader. (`evidence.*.source` stays `native`, `ocr` or `ai`: where the quote came from.)
- `readers.ai`: `status`, `need`, `calls` and `reason`. The reason is a sentence a reviewer can read: the
  `AI_REASONS` wording, the weak-scan reason that names what was replaced, or "The AI read failed: …". It is
  prefixed with the missing readers' install hint when a reader the file needed is not installed. `notes` adds
  the line-count decisions from `merge_ai_fields`.
