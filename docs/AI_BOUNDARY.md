# When Invoice Studio calls the AI

The local readers (invoice2data, PaddleOCR, Docling) read every invoice first. The AI is a boundary after them,
not a reader beside them. The code is `process()` in `app/engines.py` (the comment above `AI_REASONS`), and the
tests are the AI-boundary tests in `tests/test_reader_chain.py`.

## The item master rules run before the AI decision

After the local readers and before `need` is worked out, `app/rules_first.py` runs the rules workbook's chain
(`item_rules.rules()`) on the selected local read: the item step through `matching.lookup_item`, in the workbook's
`lookup_order` (VPN -> barcode -> description), then the tax code by country (T5.1). A rule fills only an empty
field and only on a `Found` result; a suggestion, conflict or ambiguous result fills nothing and is kept on the
`item_rules` trace entry (`held`). A line the rules identified is not an item-identity gap, so it does not send
the AI after a code. After any AI read, every rule-resolved value is put back: the AI never overwrites the item
master. A rules workbook that cannot load is told on the `item_rules` trace entry and the read goes on without it.

## What the local result asks of the AI (`readers.ai.need`)

| need | When | What the AI may change |
|---|---|---|
| `fallback` | The local result fails `_basic_checks`: no lines, no number or net, or lines that do not sum to the net. | It fills empty fields and flags every disagreement. Its lines stand in only for local lines that do not sum to a printed net (`merge_ai_fields`). On a **weak scan read** see below. |
| `gap_fill` | The local result passes the checks but leaves gaps (a missing date, an item code, a unit), or a line the rules could not identify (not in the item master, ambiguous or only a suggestion; a Conflict stays with the reviewer). | It fills empty fields only. Every value a local reader read stays, and a different AI value is flagged for review. |
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
   goes through the weak-scan rule below, which says "read the whole invoice".

## What the AI is not asked about

A wrong header value that looks fine is not a gap, so nothing asks the AI about it: the AI is asked only for what is
missing or fails the basic checks, and fills only empty fields (agreed design). Such a value stays until a person
corrects it. Turning on `ai_cross_check` flags differences on complete invoices, and still overwrites nothing.

## The weak-scan rule

It runs only in `fallback`, only on a scan, and only when `scan_guard.weak_read` finds the local read weak. A
local read that passes the basic checks is never replaced, even when it is weak (for example, no item codes): it
goes through `gap_fill`.

- If the AI's read of the page image fails its own checks (`scan_guard.ai_self_check`), nothing is filled and the
  job is `not_read`.
- Otherwise the AI **fills only the empty fields** and flags what it reads differently, with its value beside the
  local one in review. A net or tax total needs the AI's quote. Only when the local lines do not align one to one with
  the AI's (same count, `_aligned` pairwise) do the AI's lines stand in for them, every value marked `ai`.
- Example (diagnosis C2): PaddleOCR reads the net as 8.0 from `Net total: 8 800.00`. The 8.0 stays, the AI's 800 shows
  beside it for the person reviewing, and the reason ends "flagged what it reads differently (net)".

## Nothing the document does not print (owner decision D11)

`ai_context.absent_values` checks what the AI read against the document's text. A value the text does not print is
not written, and the field is left for review with a note: amounts by number, codes and words by their folded text, a
date against the printed date. A scan or a page with almost no text has nothing to check against, so there it is the
weak-scan self-check that holds. A value the AI saw only in a PDF's image (a logo's supplier name, say) is left blank
by this: strict D11.

This check is weak. A number is accepted when it is printed anywhere, so an invented 7.00 passes on a page printing
"CN-7", and text in a script other than Latin is not checked at all. A minus sign is checked: -100.00 needs a printed
`-100.00`, `(100.00)`, `100.00-` or `100.00 CR`, and 100.00 is refused where only `-100.00` is printed. The AI mark in
review and on the receipt is the real control.

## What the AI read is given

On Azure AI Foundry only (`Providers.extract` refuses it for anything else), the read also gets the rules workbook's
lookup order and item master rows near what the local read found: by printed code, by barcode, by a similar
description, by a similar supplier name, by order number. Each row is a candidate with an id. All of it is JSON inside
a tag pair it cannot close, and the model is told it is data, never instructions. The request carries no tools. A
Foundry agent carries its own, so it must have none; a reply showing anything but its message and reasoning is refused,
though by then a tool has run, so the agent's definition is the real control.

What comes back is an id. `ai_context.accept` takes the value and the source from the candidate this server sent and
refuses an id or field it did not send, so a suggestion cannot carry a value that is not in the item master.

- Supplier and order candidates, and a plain search over what the AI read, stay **suggestions** with their source. They
  fill nothing.
- An item candidate the AI names for a line's empty item becomes a rule.

## The rules the AI adds (owner, 6 Nov: no approval, they run on the fly)

`app/ai_rules.py` keeps a rule: this supplier, this printed code or description, this item master item. It runs at once
and on later invoices from that supplier, with no approval step. It holds to these limits:

- It fills only an item cell the rules left empty. A rules result of Found or Conflict stands; an Ambiguous or
  Suggestion result lets through only an item it already lists.
- A row at a site other than the supplier's is not a rule. A printed name the master does not carry resolves to no
  site (owner, 6 Nov): then only a row whose supplier name is close to the printed one makes a rule, and any other pick
  stays a suggestion. Close means the same words in the same order once legal forms and articles are
  dropped (`ai_context.LEGAL_NAME_WORDS`), and a name of only those is close to nothing. Ali Ahmed Trading is not Ahmed
  Ali Trading. L.L.C reads as LLC, but two dotted letters such as A.L. or F.Z. stay a word, and any other single letter
  or digit must match. So Gulf Trading LLC is close to Gulf
  Trading Co, but not to Gulf Foods LLC, Gulf Services LLC or Gulf LLC: a true variant with one more word stays a
  suggestion a person can accept, because a wrong suggestion writes nothing and a wrong rule keeps filling. Item
  master `SUPPLIER_NAME` holds coded site names, so most such picks stay suggestions.
- A rule is added only after the read passed its checks: a read that ends `not_read` adds none.
- A spoofed name is not caught: an invoice printing a real supplier's name can plant a rule from that supplier's own
  items under its name. It can only name that supplier's rows, and every fill is marked AI and removable. A planted
  rule fills genuine invoices with no AI call and blocks the correct rule until someone removes it.
- Its item must still be in the item master each time it runs, or it fills nothing.
- Each fill is marked `ai` in review, listed in `readers.ai.rules_applied`, and on the receipt.
- Each rule keeps its source, reason, model and file, and counts the lines it filled. **Engines & AI** lists them
  (`GET /api/ai-rules`); an administrator removes one (`DELETE /api/ai-rules/{id}`). Both changes are audited.
- With AI off, none of them runs.

The risk that comes with no approval: a wrong pick fills wrong items on later invoices until it is removed. It stays
visible as AI on every line, and `scripts/ai_fallback_compare.py` counts each rule's right and wrong lines against
truth files. `/api/rules-assistant/propose` is separate and unchanged: it still only logs a proposal.

## What every job records

- `readers.header[field]` and `readers.lines[n][field]` name the reader of each value: the engine id
  (`invoice2data`, `paddleocr`, `docling`, `native_pdf_text`), `rule:<rule id>` (e.g. `rule:I2.1`) or `ai`. A field filled from another local
  reader's read names that reader. (`evidence.*.source` stays `native`, `ocr` or `ai`: where the quote came from.)
- `readers.ai`: `status`, `need`, `calls`, `fields` and `reason`. `fields` lists the gaps the local readers and the
  item master rules left, which asked for the AI (it may fill other empty fields too, listed in `filled`); with AI off they are the fields left for the user. The reason is a sentence a reviewer can read: the
  `AI_REASONS` wording, the weak-scan reason that names what was replaced, or "The AI read failed: …". It is
  prefixed with the missing readers' install hint when a reader the file needed is not installed. `notes` adds
  the line-count decisions from `merge_ai_fields`.
