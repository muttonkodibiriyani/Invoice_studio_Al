"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

// Derived from owner sheets by the fine rules; on fine-rules jobs they are shown with evidence, not typed.
const RULES_DERIVED_FIELDS = new Set(["seller", "site", "buyer", "location", "origin", "market", "taxCode"]);

function rulesJob(job) {
  return job?.validation?.source === "fine_rules";
}

function evidenceText(evidence) {
  return (evidence || []).map((entry) => {
    const where = entry.kind === "sheet" ? `owner sheet ${entry.reference}`
      : entry.kind === "table" ? `${entry.source} · ${entry.reference}`
        : entry.kind === "owner_rule" ? (/^owner rule\b/i.test(entry.reference || "") ? entry.reference : `owner rule · ${entry.reference}`)
        : entry.kind === "selected" ? `selected by rule, not printed · ${entry.reference}`
        : entry.kind === "owner_entry" && entry.rule === "OWNER-PICK" ? "from the reviewer's supplier-code pick"
        : entry.kind === "owner_entry" ? `entered by reviewer${entry.original ? ` · ${entry.original}` : ""}`
        : entry.kind === "printed" ? `printed on invoice${entry.reference ? ` · ${entry.reference}` : ""}${entry.original ? ` · “${entry.original}”` : ""}`
          : `${entry.source}${entry.reference ? ` · ${entry.reference}` : ""}`;
    return `${where}${entry.rule && !where.includes(entry.rule) ? ` (${entry.rule})` : ""}`;
  }).join("; ");
}

// Target cells the download cannot be written without; when the rules leave one empty the reviewer may enter it.
const RULES_REQUIRED_HEADER = new Set(["number", "site", "po", "location", "location_type", "date", "net", "tax", "taxCode"]);
const RULES_REQUIRED_LINE = new Set(["Item", "Unit Cost", "Quantity", "Unit Tax Code"]);
const RULES_ENTRY_HINT = { location_type: "Store (S) or Warehouse (W)", date: "YYYY-MM-DD" };

// A site the rules filled from the reviewer's supplier-code pick is not a typed entry: "Clear pick" undoes it.
function enteredByReviewer(cell) {
  return (cell?.evidence || []).some((entry) => entry.kind === "owner_entry" && entry.rule !== "OWNER-PICK");
}

function rulesEntry(cell, label, scope, name, line) {
  const input = make("input", "rules-entry");
  input.dataset.scope = scope;
  input.dataset.name = name;
  if (line) input.dataset.line = String(line);
  input.value = enteredByReviewer(cell) ? cell.value : "";
  input.placeholder = RULES_ENTRY_HINT[name] || "Enter if you have it";
  input.setAttribute("aria-label", `${label}${line ? ` line ${line}` : ""}, entered by reviewer`);
  input.disabled = app.currentJob?.status === "exported";
  return input;
}

function rulesCell(cell, name, line) {
  const td = make("td");
  const empty = !cell || cell.value === null || cell.value === undefined;
  if (empty) td.append(make("span", "rules-flagged", cell?.reason === "No evidence" ? "Empty · no evidence" : cell?.reason || "Not found"));
  else td.append(make("span", "", cell.value), make("small", "", evidenceText(cell.evidence)));
  td.append(...readerNotes(app.currentJob, "line", name, line));
  if ((empty && RULES_REQUIRED_LINE.has(name)) || enteredByReviewer(cell)) td.append(rulesEntry(cell, name, "line", name, line));
  return td;
}

// Which reader produced a field and where readers disagreed (TRAIN's job.readers / evidence.*.review).
// Both are keyed by invoice field names; line cells map from the target column. Absent keys show nothing.
const READER_LINE_FIELDS = { Item: "sku", UPC: "gtin", "Unit Cost": "price", Quantity: "qty" };
const READER_HEADER_FIELDS = { site: "supplier_name" };
const AI_READER_LABELS = {
  fallback: "Fallback", gap_fill: "Gap fill", cross_check: "Cross-check", skipped: "Not needed", off: "Off",
  unavailable: "Not connected", failed: "Failed", selected: "Selected reader", not_read: "Read rejected",
};

// Why a reader set a value aside or left it empty (evidence.*.review), worded by review.code (decision 145). A review
// without a known code reads "Check": the client cannot tell readers disagreeing from a P.O. Box or filename flag.
const REVIEW_LABELS = {
  not_read: "Not read", not_printed: "Not printed", misprint: "Barcode misprint",
  unreadable: "Barcode unreadable", unconfirmed: "Barcode not confirmed",
};
const LINE_FIELD_LABELS = { net_amount: "Line net", tax_amount: "Line tax", description: "Description", uom: "Unit", page: "Page" };

function reviewNote(review, field = "") {
  if (!review?.reason) return null;
  const other = review.other_value === null || review.other_value === undefined || review.other_value === "" ? "" : ` · other read: ${review.other_value}`;
  const page = review.other_page ? ` (page ${review.other_page})` : "";
  const note = make("small", "rules-flagged review-note", `${field ? `${field} · ` : ""}${REVIEW_LABELS[review.code] || "Check"}: ${review.reason}${other}${page}`);
  if (review.code) note.dataset.reviewCode = review.code;
  if (review.other_quote) note.title = review.other_quote;
  return note;
}

// evidence.lines is positional and a review save leaves it as read, so once a line is added or removed its reviews
// would sit beside the wrong row: they show only while the counts still match.
function lineReviews(job) {
  const lines = job?.evidence?.lines;
  return Array.isArray(lines) && lines.length === (job?.invoice?.lines || []).length ? lines : null;
}
// A "not read" or "not printed" note explains an EMPTY value, so it hides while the reviewer's own entry fills the input
// (decision 156); barcode notes (misprint, unreadable, unconfirmed) stay, since the printed code is still in doubt.
const EMPTY_VALUE_CODES = new Set(["not_read", "not_printed"]);
function noteFor(input, note) {
  if (note && EMPTY_VALUE_CODES.has(note.dataset.reviewCode)) {
    const sync = () => { note.hidden = String(input.value ?? "").trim() !== ""; };
    input.addEventListener("input", sync);
    sync();
  }
  return note;
}
const hasLineReview = (job) => (job?.evidence?.lines || []).some((entry) => Object.values(entry || {}).some((value) => value?.review?.reason));

function readerNotes(job, scope, column, line) {
  const key = scope === "header" ? READER_HEADER_FIELDS[column] || column : READER_LINE_FIELDS[column];
  const readers = scope === "header" ? job?.readers?.header : job?.readers?.lines?.[line - 1];
  const evidence = scope === "header" ? job?.evidence?.header : lineReviews(job)?.[line - 1];
  const notes = [];
  const badge = readerBadge(readers?.[key]);
  if (badge) notes.push(badge);
  const note = reviewNote(evidence?.[key]?.review);
  if (note) notes.push(note);
  return notes;
}

// The reader of one value (job.readers.header / lines: an engine id or "ai"), so no AI value passes as a local read.
function readerBadge(reader) {
  if (typeof reader !== "string" || !reader) return null;
  const badge = make("span", `reader-badge reader-${reader}`, reader === "ai" ? "AI" : humanize(reader));
  badge.title = reader === "ai" ? "Filled by the AI: check it against the document" : `Read by ${humanize(reader)}`;
  return badge;
}

// readers.lines is positional like evidence.lines, so it marks the rows only while the counts still match.
function lineReaders(job) {
  const lines = job?.readers?.lines;
  return Array.isArray(lines) && lines.length === (job?.invoice?.lines || []).length ? lines : null;
}

// Why the AI ran on this invoice (engines first, then the AI per field), how many calls it made, and any local
// reader that was not installed with how to install it, unless the reason already starts with it
// (the server prefixes "paddleocr, docling: not installed. Run …" when a missing reader is why the AI ran).
function renderAiReader(job) {
  const box = $("#ai-reader");
  const ai = job?.readers?.ai;
  const reason = String(ai?.reason || "");
  const named = reason.split(":")[0].split(", ");
  const missing = (job?.readers?.not_installed || [])
    .filter((entry) => entry?.hint && !(named.includes(entry.engine) && reason.includes(entry.hint.slice(1))));
  box.hidden = !ai?.status && !missing.length;
  box.replaceChildren();
  if (box.hidden) return;
  const used = ["fallback", "gap_fill", "cross_check", "selected"].includes(ai?.status);
  box.className = `ai-reader${used ? " ai-used" : ""}`;
  if (ai?.status) {
    const calls = Number(ai.calls) || 0;
    box.append(make("strong", "", `AI reader: ${AI_READER_LABELS[ai.status] || humanize(ai.status)} · ${calls} call${calls === 1 ? "" : "s"}`));
    if (reason) box.append(make("span", "", `${used ? "Why the AI ran" : "Why"}: ${reason}`));
    box.title = (ai.notes || []).join("\n");
  }
  missing.forEach((entry) => box.append(make("span", "", `${humanize(entry.engine)}: ${entry.hint}`)));
}

const TARGET_GROUPS = [
  ["needs_checking", "Needs checking"],
  ["empty_flagged", "Empty (flagged)"],
  ["verified", "Verified"],
  ["empty_owner_rule", "Empty by owner rule"],
];
const TARGET_STATUS_LABELS = {
  verified: "Verified", empty_owner_rule: "Empty by owner rule", empty_flagged: "Empty (flagged)", mismatch: "Mismatch",
  over_cited: "Over-cited", no_evidence: "No evidence", unverifiable: "Unverifiable", data_gap: "Data gap",
  owner_entry_unattributed: "Owner entry not attributed",
};

function targetCellName(cell) {
  return `${cell.sheet}.${cell.column}${cell.line ? ` line ${cell.line}` : ""}`;
}

function targetEvidence(evidence = {}) {
  return [evidence.source, evidence.reference, evidence.rule].filter(Boolean).join(" · ") || "No evidence cited";
}

// The per-invoice target-sheet check: the owner line, the checks, and every cell by status with its evidence.
function renderTargetCheck(job) {
  const panel = $("#target-check");
  const check = job.rules?.target_check;
  panel.hidden = !rulesJob(job) || !check?.cells;
  if (panel.hidden) return;
  $("#target-check-line").textContent = check.summary;
  const failed = (check.checks || []).filter((item) => item.status === "fail");
  const hold = $("#target-check-hold");
  const emptyCells = Number(check.counts?.empty_flagged || 0);
  // "Target sheet OK" with cells still to fill reads as a pass the sheet has not earned: an invoice
  // whose lines were not read has empty cells, which is not the same as a cell that was checked.
  hold.textContent = check.holds
    ? (job.status === "ready" || job.status === "exported" ? "Confirmed by owner" : "Held at review: confirm after checking")
    : emptyCells
      ? `${emptyCells} cell${emptyCells === 1 ? "" : "s"} still empty`
      : "Target sheet OK";
  hold.className = `status-pill ${(check.holds || emptyCells) && !["ready", "exported"].includes(job.status) ? "warning" : "success"}`;
  panel.classList.toggle("holds", Boolean(check.holds));
  if (panel.dataset.job !== job.id) panel.open = Boolean(check.holds) && !["ready", "exported"].includes(job.status);
  panel.dataset.job = job.id;

  const checks = $("#target-check-checks");
  checks.replaceChildren();
  (check.checks || []).forEach((item) => {
    const row = make("p", `target-check-row ${item.status}`);
    row.append(make("span", `status-pill ${item.status === "fail" ? "error" : item.status === "pass" ? "success" : "warning"}`, item.status),
      make("strong", "", humanize(item.check)), make("small", "", item.detail || ""));
    checks.append(row);
  });
  if (failed.length) checks.prepend(make("p", "rules-flagged", `${failed.length} check${failed.length === 1 ? "" : "s"} failed`));

  const cells = $("#target-check-cells");
  cells.replaceChildren();
  TARGET_GROUPS.forEach(([group, label]) => {
    const picked = check.cells.filter((cell) => cell.group === group);
    if (!picked.length) return;
    const details = make("details", `target-group ${group}`);
    details.open = group === "needs_checking";
    details.append(make("summary", "", `${label} · ${picked.length}`));
    const table = make("table", "line-table target-cells");
    const head = make("tr");
    ["Cell", "Status", "Value", "Reason", "Evidence"].forEach((name) => head.append(make("th", "", name)));
    table.append(make("thead"), make("tbody"));
    table.tHead.append(head);
    picked.forEach((cell) => {
      const tr = make("tr");
      const status = TARGET_STATUS_LABELS[cell.status] || humanize(cell.status);
      tr.append(make("td", "", targetCellName(cell)), make("td", "", cell.sub === "owner_entry" ? `${status} (owner entry)` : status),
        make("td", "", cell.value || "—"), make("td", "", cell.reason || ""), make("td", "", targetEvidence(cell.evidence)));
      table.tBodies[0].append(tr);
    });
    const scroll = make("div", "table-scroll");
    scroll.append(table);
    details.append(scroll);
    cells.append(details);
  });
}

// Inbox indicator from the trimmed target_check (counts and the failed check names, no values).
function targetChip(job) {
  const check = job.rules?.target_check;
  if (!check?.counts) return null;
  const waiting = Number(check.counts.needs_checking || 0);
  const empty = Number(check.counts.empty_flagged || 0);
  const failed = check.failed || [];
  const confirmed = ["ready", "exported"].includes(job.status);
  // Say what the sheet is. "Target check failed" named the check itself as the fault, when what it
  // had found was a row check that could not hold - most often because the read produced no item
  // lines at all. Cells to check, cells still empty and a failed row check are three different things.
  const text = confirmed && !waiting ? "Target confirmed"
    : waiting ? `${waiting} to check`
    : failed.length ? `${failed.length} row check${failed.length === 1 ? "" : "s"} failed`
    : empty ? `${empty} cell${empty === 1 ? "" : "s"} empty`
    // A check stored before #41 names no failed row check, but still holds the job at review.
    : check.holds ? "Held at review"
    : "Target verified";
  const outstanding = waiting || empty || failed.length || check.holds;
  const chip = make("span", `target-chip ${!outstanding || (confirmed && !waiting) ? "success" : "warning"}`, text);
  chip.title = [check.summary, failed.length ? `Row checks: ${failed.map(humanize).join(", ")}` : ""]
    .filter(Boolean).join(" \u00b7 ");
  return chip;
}

function accuracyPercent(rate) {
  return rate?.accuracy === null || rate?.accuracy === undefined ? "—" : `${(Number(rate.accuracy) * 100).toFixed(1)}%`;
}

function accuracyRows(body, rates, label = (key) => key) {
  body.replaceChildren();
  const entries = Object.entries(rates || {});
  if (!entries.length) {
    const tr = make("tr");
    const td = make("td", "table-empty", "No confirms in this period.");
    td.colSpan = 5;
    tr.append(td);
    body.append(tr);
  }
  entries.forEach(([key, rate]) => {
    const tr = make("tr");
    tr.append(make("td", "", label(key) || "—"), make("td", "", String(rate.cells)), make("td", "", String(rate.unchanged)),
      make("td", "", String(rate.changed)), make("td", "", accuracyPercent(rate)));
    body.append(tr);
  });
}

async function loadAccuracy() {
  const select = $("#accuracy-supplier");
  const supplier = select.value;
  const summary = await api(`/api/target-check/accuracy${supplier ? `?supplier=${encodeURIComponent(supplier)}` : ""}`);
  if (!supplier) {
    const codes = Object.keys(summary.periods?.all?.suppliers || {}).filter(Boolean);
    select.replaceChildren(make("option", "", "All suppliers"), ...codes.map((code) => make("option", "", code)));
    select.options[0].value = "";
  }
  $$("[data-accuracy-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.accuracyPeriod === app.accuracyPeriod)));
  const period = summary.periods?.[app.accuracyPeriod] || {};
  const overall = period.overall || {};
  $("#accuracy-overall").textContent = period.confirms
    ? `${period.confirms} confirmed invoice${period.confirms === 1 ? "" : "s"} · ${overall.cells} cells · ${overall.unchanged} unchanged by the owner · accuracy ${accuracyPercent(overall)}`
    : "No confirmed invoices in this period yet.";
  accuracyRows($("#accuracy-fields"), period.fields);
  accuracyRows($("#accuracy-statuses"), period.by_status_before, (key) => TARGET_STATUS_LABELS[key] || humanize(key));
  accuracyRows($("#accuracy-suppliers"), period.suppliers);
}

function collectEntries() {
  const entries = { header: {}, lines: {} };
  document.querySelectorAll(".rules-entry").forEach((input) => {
    const value = input.value.trim();
    if (!value) return;
    if (input.dataset.scope === "header") entries.header[input.dataset.name] = value;
    else (entries.lines[input.dataset.line] ||= {})[input.dataset.name] = value;
  });
  return entries;
}

// An empty header field shows the header-level exception raised for it, e.g. R-016's reason under Tax code.
function headerException(job, target) {
  return (job.validation?.issues || []).find((issue) => target && issue.code === target && !issue.line && issue.message)?.message || "";
}

function renderRulesResult(job) {
  const rules = job.rules;
  const show = rulesJob(job) && Boolean(rules);
  $("#rules-fieldset").hidden = !show;
  if (!show) return;
  const rate = rules.item_lines || {};
  const percent = rate.rate === null || rate.rate === undefined ? "—" : `${(Number(rate.rate) * 100).toFixed(1)}%`;
  const itemRate = $("#rules-item-rate");
  itemRate.textContent = `Item lines resolved ${rate.resolved ?? 0}/${rate.total ?? 0} (${percent})`
    + (rate.owner_review ? " — below 95%: this invoice goes to owner review." : "");
  itemRate.title = rate.definition || "";
  itemRate.classList.toggle("owner-review", Boolean(rate.owner_review));
  const list = $("#rules-fields");
  list.replaceChildren();
  Object.entries(rules.fields || {}).forEach(([key, field]) => {
    const dd = make("dd");
    const empty = field.value === null || field.value === undefined;
    if (empty) {
      dd.append(make("span", "rules-flagged", field.reason === "No evidence" ? "Empty · no evidence"
        // "Not found" is the view's placeholder for every blank field, not a reason of its own.
        : (field.reason !== "Not found" && field.reason) || headerException(job, field.target) || "Not found in owner sheets or on the invoice"));
    } else dd.append(make("span", "", field.value), make("small", "", evidenceText(field.evidence)));
    dd.append(...readerNotes(job, "header", key));
    if ((empty && RULES_REQUIRED_HEADER.has(key)) || enteredByReviewer(field)) dd.append(rulesEntry(field, field.label, "header", key));
    if (key === "site") renderSupplierPick(dd, job);
    if (field.target === "Order No" && Number(rules.po_candidates) > 1) {
      dd.append(make("small", "rules-flagged", `Ambiguous: ${rules.po_candidates} candidate orders — owner review`));
    }
    list.append(make("dt", "", field.label), dd);
  });
  const body = $("#rules-lines");
  body.replaceChildren();
  (rules.lines || []).forEach((line) => {
    const tr = make("tr");
    const number = make("td", "", String(line.line));
    // Line fields with no column here (line net, description, ...) show their reviews beside the line number.
    const shown = new Set(Object.values(READER_LINE_FIELDS));
    Object.entries(lineReviews(job)?.[line.line - 1] || {}).forEach(([field, entry]) => {
      const note = shown.has(field) ? null : reviewNote(entry?.review, LINE_FIELD_LABELS[field] || field);
      if (note) number.append(note);
    });
    tr.append(number);
    ["Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code"].forEach((name) => tr.append(rulesCell(line.cells?.[name], name, line.line)));
    body.append(tr);
  });
}

// When the rules leave 2+ supplier codes, the reviewer picks one: a click saves the form with that code and the
// rules re-run on it (decision 41). The candidates stay listed with the pick marked; "Clear pick" sends "".
function renderSupplierPick(dd, job) {
  const candidates = job.rules?.supplier_site_candidates || [];
  if (candidates.length < 2) return;
  const picked = job.owner_supplier_code ? String(job.owner_supplier_code) : "";
  const locked = ["queued", "processing", "exported"].includes(job.status);
  const box = make("div", "supplier-pick");
  box.append(make("small", "", picked
    ? `Supplier code ${picked} picked at review · the rules filled the site from it`
    : `${candidates.length} supplier codes match this supplier · pick the right one`));
  const options = make("div", "supplier-pick-options");
  candidates.forEach((candidate) => {
    const code = String(candidate.supplier_code ?? "");
    const sites = candidate.sites || [];
    const first = sites[0] || {};
    const button = make("button", `supplier-pick-option${code === picked ? " picked" : ""}`);
    button.type = "button";
    button.dataset.supplierCode = code;
    button.setAttribute("aria-pressed", String(code === picked));
    button.setAttribute("aria-label", `Pick supplier code ${code}`);
    const where = [first.site_name || first.entity || "", sites.length > 1 ? `+${sites.length - 1} more` : ""].filter(Boolean).join(" ");
    button.append(make("strong", "", code), make("span", "", where || candidate.location || ""));
    button.title = [candidate.reason, ...sites.map((site) => [site.supplier_site, site.site_name].filter(Boolean).join(" · "))].filter(Boolean).join("\n");
    button.disabled = locked;
    button.addEventListener("click", () => { if (code !== picked) pickSupplier(code); });
    options.append(button);
  });
  if (picked) {
    const clear = make("button", "button button-quiet supplier-pick-clear", "Clear pick");
    clear.type = "button";
    clear.disabled = locked;
    clear.addEventListener("click", () => pickSupplier(""));
    options.append(clear);
  }
  box.append(options);
  dd.append(box);
}

async function pickSupplier(code) {
  $$(".supplier-pick button").forEach((button) => { button.disabled = true; });
  const done = code ? `Supplier code ${code} picked. The rules re-ran with it.` : "Supplier code pick cleared. The rules re-ran.";
  if (!(await submitReview({ supplier_code: code, confirm: false }, done))) {
    $$(".supplier-pick button").forEach((button) => { button.disabled = false; });
  }
}

function draftValue(job, field, fallback) {
  if (!rulesJob(job)) return fallback;
  return job.rules?.fields?.[field]?.value ?? "";
}

// On rules jobs the Buyer name shows the owner rule BUYER-NAME value, which the target sheet's Buyer uses; the
// reader's printed Bill To stays stored and shows as a hint when it differs. Display only, like draftValue().
function rulesBuyer(job) {
  const field = rulesJob(job) ? job.rules?.fields?.buyer_name : null;
  return field && field.value !== null && field.value !== undefined && field.value !== "" ? field : null;
}

function buyerEvidence(field) {
  const entry = field.evidence?.[0];
  if (entry?.kind === "printed") return `printed${entry.reference ? `, ${entry.reference}` : ""}`;
  if (entry?.kind === "owner_rule") return `owner rule ${entry.rule || "BUYER-NAME"}`;
  return evidenceText(field.evidence);
}

// Decision 62: the reader's Purchase order is not Order No. When POG-001 found the order in POGRN, the review shows
// that Order No beside it with its source, so an empty Purchase order does not read as "no order". Display only.
function pogrnOrder(job) {
  const field = rulesJob(job) ? job.rules?.fields?.po : null;
  if (!field || field.value === null || field.value === undefined || field.value === "") return null;
  const entry = (field.evidence || []).find((item) => (item.rule === "POG-001" && ["printed", "selected"].includes(item.kind))
    || (item.rule === "POG-008" && item.kind === "sheet"));
  if (!entry) return null;
  // Decisions 68/69/78: POG-008 fills Order No only from the one unreceived order holding every invoiced item and qty.
  const source = entry.rule === "POG-008" ? "POGRN order matched on items and ordered qty, not yet received"
    : entry.kind === "printed" ? "Invoice PO / Reference # found in POGRN as RMS_ORDER_NO under the supplier code"
      : "POGRN order selected by qty and value under the supplier code, not printed";
  return { value: String(field.value), source };
}

// Decisions 76/82/83: Order Date (POG-009, CREATED_DATE) and Receipt Date(s) (POG-010, RECEIPT_DATE) are evidence only,
// the text of the POGRN rows cited for Order No. The note formats ISO dates for display; the exact cell text stays in
// the tooltip. An empty value shows its reason. The POG-010 flag itself shows as the non-blocking 'Date Check' issue.
function displayDate(raw) {
  const iso = /^(\d{4}-\d{2}-\d{2})(?:[T ]00:00:00)?$/.exec(raw.trim());
  const parsed = iso ? new Date(`${iso[1]}T00:00:00Z`) : null;
  return parsed && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleDateString([], { dateStyle: "medium", timeZone: "UTC" }) : raw;
}

function orderDateNote(job) {
  if (!rulesJob(job)) return null;
  const parts = [];
  const titles = [];
  const add = (label, evidence, split) => {
    if (!evidence) return;
    const raw = evidence.value === null || evidence.value === undefined ? "" : String(evidence.value);
    if (!raw) {
      parts.push(`${label} empty (${evidence.reason || "no POGRN date"})`);
      return;
    }
    const values = split ? raw.split(", ") : [raw];
    parts.push(`${label} ${values.map(displayDate).join(", ")}`);
    titles.push(`${label}: ${raw}`);
  };
  add("Order Date", job.rules?.order_date, false);
  add("Receipt Date(s)", job.rules?.receipt_date, true);
  if (!parts.length) return null;
  const reference = job.rules?.order_date?.reference || job.rules?.receipt_date?.reference;
  return { text: `${parts.join(" · ")} · POGRN rows cited for Order No, evidence only`,
    title: titles.length ? [...titles, reference].filter(Boolean).join(" · ") : "" };
}

const foldText = (value) => String(value ?? "").replace(/\s+/g, " ").trim().toLowerCase();

const HEADER_FIELDS = [
  ["number", "Invoice number", "text"],
  ["supplier_name", "Supplier name", "text"],
  ["buyer_name", "Buyer name · Bill To", "text"],
  ["seller", "Seller entity", "text"],
  ["site", "Supplier site", "text"],
  ["buyer", "Buyer code · reference", "text"],
  ["po", "Purchase order", "text"],
  ["location", "Delivery location", "text"],
  ["date_printed", "Printed invoice date · source", "text", { readOnly: true }],
  ["date", "Invoice date · confirm calendar date", "date"],
  ["currency", "Currency", "text"],
  ["origin", "Origin", "text"],
  ["market", "Market", "text"],
  ["taxCode", "Tax code", "text"],
  ["net", "Net total", "number"],
  ["tax", "Tax total", "number"],
];

const DRAFT_HEADER_COLUMNS = ["Transaction Number", "Document", "Supplier Site", "Order No", "Location", "Location Type", "Document Date", "Total Cost Ex Tax", "Tax Amount", "Ref No. 1", "Ref No. 2", "Ref No. 3", "Comment"];
const DRAFT_TAX_COLUMNS = ["Transaction Number", "Tax Code", "Tax Basis"];
const DRAFT_DETAIL_COLUMNS = ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code"];

const STATUS_LABELS = {
  queued: "Queued",
  processing: "Processing",
  review: "Needs review",
  ready: "Ready to export",
  exported: "Exported",
  error: "Needs attention",
};

const NATIVE_TEXT_PREFERENCE = "invoice-studio-prefer-native-text";
const SIDEBAR_PREFERENCE = "invoice-studio-sidebar";
const PANE_SIZE_PREFERENCE = "invoice-studio-pane-sizes";
// Below this width the sidebar starts compact unless the user expanded it.
const COMPACT_SIDEBAR_QUERY = "(max-width: 1599px)";

function savedNativeTextPreference() {
  try {
    const saved = window.localStorage.getItem(NATIVE_TEXT_PREFERENCE);
    return { value: saved === null ? true : saved !== "false", explicit: saved !== null };
  } catch (_) {
    return { value: true, explicit: false };
  }
}

const initialNativeTextPreference = savedNativeTextPreference();

const app = {
  state: null,
  currentJob: null,
  selectedJobId: null,
  selectedForBatch: new Set(),
  inboxSort: "newest",
  accuracyPeriod: "all",
  reviewDirty: false,
  uploadFiles: [],
  batch: null,
  pollTimer: null,
  pollBusy: false,
  pollCursor: 0,
  messageTimer: null,
  selectionToken: 0,
  preflight: null,
  authMode: "unknown",
  sessionUser: null,
  sessionVerified: false,
  workspaceStarted: false,
  documentUrl: null,
  documentJobId: null,
  documentToken: 0,
  manualDraftJobId: null,
  referenceLookupLoaded: false,
  referenceLookupCursor: null,
  referenceLookupQuery: "",
  referenceLookupKind: "item",
  referenceLookupFilters: {},
  referenceLookupTarget: null,
  managedVertexModel: "",
  managedVertexModelsLoaded: false,
  providerModels: {},
  providerVerified: {},
  modelLoadTokens: {},
  preferNativeText: initialNativeTextPreference.value,
  nativeTextPreferenceExplicit: initialNativeTextPreference.explicit,
};

function setNativeTextPreference(value, { remember = true } = {}) {
  app.preferNativeText = Boolean(value);
  ["upload", "retry"].forEach((prefix) => {
    const input = $(`#${prefix}-prefer-native-text`);
    if (input) input.checked = app.preferNativeText;
  });
  if (!remember) return;
  app.nativeTextPreferenceExplicit = true;
  try { window.localStorage.setItem(NATIVE_TEXT_PREFERENCE, String(app.preferNativeText)); }
  catch (_) { /* Browser storage is optional; the in-memory preference remains. */ }
}

function savedLayoutPreference(key) {
  try { return window.localStorage.getItem(key); } catch (_) { return null; }
}

function rememberLayoutPreference(key, value) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch (_) { /* Browser storage is optional; the layout still applies to this page. */ }
}

function applySidebarMode() {
  const saved = savedLayoutPreference(SIDEBAR_PREFERENCE);
  const compact = saved ? saved === "compact" : window.matchMedia(COMPACT_SIDEBAR_QUERY).matches;
  $("#app-shell").classList.toggle("nav-compact", compact);
  const toggle = $("#sidebar-toggle");
  const label = compact ? "Expand menu" : "Collapse menu";
  toggle.setAttribute("aria-expanded", String(!compact));
  toggle.title = label;
  toggle.querySelector("span").textContent = label;
}

// Inbox width is stored in pixels; the document pane as a share of the
// document + details row. CSS still caps the inbox at 40% of the workspace.
const PANE_LIMITS = {
  inbox: { property: "--inbox-width", unit: "px", min: 240, max: 520, step: 16 },
  evidence: { property: "--evidence-share", unit: "%", min: 25, max: 60, step: 2 },
};

function savedPaneSizes() {
  try { return JSON.parse(savedLayoutPreference(PANE_SIZE_PREFERENCE) || "{}") || {}; } catch (_) { return {}; }
}

function setupPaneResizers() {
  const saved = savedPaneSizes();
  $$("[data-pane-resizer]").forEach((handle) => {
    const name = handle.dataset.paneResizer;
    const limits = PANE_LIMITS[name];
    const container = handle.parentElement;
    const pane = name === "inbox" ? $(".inbox-card") : $(".evidence-card");
    handle.setAttribute("aria-valuemin", String(limits.min));
    handle.setAttribute("aria-valuemax", String(limits.max));
    const measure = () => {
      const width = pane.getBoundingClientRect().width;
      return name === "inbox" ? width : (width / container.getBoundingClientRect().width) * 100;
    };
    const apply = (value) => {
      if (value === null) {
        container.style.removeProperty(limits.property);
        handle.removeAttribute("aria-valuenow");
        return null;
      }
      const clamped = Math.round(Math.min(limits.max, Math.max(limits.min, value)));
      container.style.setProperty(limits.property, `${clamped}${limits.unit}`);
      handle.setAttribute("aria-valuenow", String(clamped));
      return clamped;
    };
    const save = (value) => {
      const sizes = savedPaneSizes();
      if (value === null) delete sizes[name];
      else sizes[name] = value;
      rememberLayoutPreference(PANE_SIZE_PREFERENCE, JSON.stringify(sizes));
    };
    if (Number.isFinite(saved[name])) apply(saved[name]);

    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add("pane-resizing");
      let latest = null;
      const move = (moveEvent) => {
        const box = container.getBoundingClientRect();
        const offset = moveEvent.clientX - box.left;
        latest = apply(name === "inbox" ? offset : (offset / box.width) * 100);
      };
      const stop = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", stop);
        handle.removeEventListener("pointercancel", stop);
        document.body.classList.remove("pane-resizing");
        if (latest !== null) save(latest);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", stop);
      handle.addEventListener("pointercancel", stop);
    });
    handle.addEventListener("keydown", (event) => {
      const delta = { ArrowLeft: -limits.step, ArrowRight: limits.step }[event.key];
      if (!delta) return;
      event.preventDefault();
      save(apply(measure() + delta));
    });
    handle.addEventListener("dblclick", () => save(apply(null)));
  });
}

async function studioFetch(path, options = {}) {
  const config = { ...options, headers: { ...(options.headers || {}) } };
  if (config.method && config.method !== "GET") config.headers["X-Studio-Request"] = "1";
  if (config.body && !(config.body instanceof FormData) && typeof config.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  if (app.authMode === "cloud" && path.startsWith("/api/")) {
    const token = await window.InvoiceStudioAuth.getToken();
    config.headers.Authorization = `Bearer ${token}`;
  }
  try {
    return await fetch(path, config);
  } catch (_) {
    throw new Error("Invoice Studio could not reach the server.");
  }
}

// Bulk downloads over many invoices outlast the hosting proxy's 60 s: the server builds them in the background and
// each request waits under 60 s; a 202 means "still building", so the next poll picks it up.
async function studioBackground(kind, body) {
  let response = await studioFetch(`/api/background/${kind}`, { method: "POST", body });
  while (response.status === 202) {
    const { id } = await response.json();
    response = await studioFetch(`/api/background/tasks/${id}`, { method: "GET" });
  }
  return response;
}

async function responseError(response) {
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  const detail = body && typeof body === "object" ? body.detail : body;
  const message = typeof detail === "string" ? detail : detail?.message
    || (Array.isArray(detail) ? detail.map((issue) => issue.msg || "Invalid value").join("; ") : "");
  const error = new Error(message || `Request failed (${response.status})`);
  error.detail = detail;
  error.status = response.status;
  return error;
}

async function api(path, options = {}) {
  const response = await studioFetch(path, options);
  if (!response.ok) throw await responseError(response);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  return body;
}

async function apiBlob(path) {
  const response = await studioFetch(path, { method: "GET" });
  if (!response.ok) throw await responseError(response);
  return { blob: await response.blob(), disposition: response.headers.get("content-disposition") || "" };
}

function dispositionFilename(disposition, fallback) {
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try { return decodeURIComponent(encoded); } catch (_) { return fallback; }
  }
  return disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
}

async function downloadApi(path, fallbackName = "download") {
  const { blob, disposition } = await apiBlob(path);
  saveBlob(blob, dispositionFilename(disposition, fallbackName));
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function setDraftField(sectionName, column, value) {
  const section = $(`[data-draft-section="${sectionName}"]`, $("#manual-draft-form"));
  const input = [...section.elements].find((element) => element.name === column);
  if (input) input.value = value ?? "";
}

function addManualDraftLine(line = {}) {
  const row = make("tr");
  row.dataset.lookupDescription = line._description || "";
  const values = {
    "Transaction Number": line["Transaction Number"] ?? 1,
    Item: line.Item ?? "",
    UPC: line.UPC ?? "",
    "Unit Cost": line["Unit Cost"] ?? "",
    Quantity: line.Quantity ?? "",
    "Unit Tax Code": line["Unit Tax Code"] ?? "",
  };
  DRAFT_DETAIL_COLUMNS.forEach((column) => {
    const cell = make("td");
    const input = make("input");
    input.name = column;
    input.value = values[column];
    input.required = true;
    if (column === "Transaction Number") { input.type = "number"; input.min = "1"; input.max = "1"; input.step = "1"; }
    if (["Unit Cost", "Quantity"].includes(column)) {
      input.type = "number";
      input.step = "0.0001";
      input.min = column === "Unit Cost" ? "0" : "0.0001";
    }
    input.addEventListener("input", updateManualDraftTotal);
    cell.append(input);
    row.append(cell);
  });
  const find = make("button", "line-lookup", "Find item");
  find.type = "button";
  find.addEventListener("click", () => beginLineReferenceLookup("draft", row));
  row.children[1].append(find);
  const action = make("td");
  const remove = make("button", "remove-line", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove draft detail row");
  remove.addEventListener("click", () => {
    if ($$("tr", $("#manual-draft-lines")).length === 1) return;
    row.remove();
    updateManualDraftTotal();
  });
  action.append(remove);
  row.append(action);
  $("#manual-draft-lines").append(row);
  updateManualDraftTotal();
}

function updateManualDraftTotal() {
  const total = $$("tr", $("#manual-draft-lines")).reduce((sum, row) => {
    const cost = Number($('[name="Unit Cost"]', row).value);
    const quantity = Number($('[name="Quantity"]', row).value);
    return sum + (Number.isFinite(cost) && Number.isFinite(quantity) ? cost * quantity : 0);
  }, 0);
  $("#manual-draft-total").textContent = `Detail total: ${total.toFixed(4)}`;
}

function initializeManualDraft(job) {
  const invoice = structuredClone(job.invoice || {});
  const matches = job.validation?.matches || [];
  setDraftField("Header", "Transaction Number", 1);
  setDraftField("Header", "Document", invoice.number);
  setDraftField("Header", "Supplier Site", draftValue(job, "site", invoice.site));
  setDraftField("Header", "Order No", draftValue(job, "po", invoice.po));
  setDraftField("Header", "Location", draftValue(job, "location", invoice.location));
  setDraftField("Header", "Location Type", job.validation?.location_type);
  setDraftField("Header", "Document Date", draftValue(job, "date", invoice.date));
  setDraftField("Header", "Total Cost Ex Tax", draftValue(job, "net", invoice.net));
  setDraftField("Header", "Tax Amount", draftValue(job, "tax", invoice.tax));
  setDraftField("Header", "Ref No. 1", "");
  setDraftField("Header", "Ref No. 2", "");
  setDraftField("Header", "Ref No. 3", "");
  setDraftField("Header", "Comment", "DRAFT UNVALIDATED — manual entry");
  setDraftField("Tax_Breakdown", "Transaction Number", 1);
  setDraftField("Tax_Breakdown", "Tax Code", draftValue(job, "taxCode", invoice.taxCode));
  setDraftField("Tax_Breakdown", "Tax Basis", draftValue(job, "net", invoice.net));
  $("#manual-draft-lines").replaceChildren();
  const matchFor = (index) => matches.find((match) => match.line === index + 1);
  // With matches, Item is what the target workbook writes for the line: the rules' match only.
  // A typed item_id stays a review candidate (ITM-006); it fills Item only when there are no matches.
  const typedItem = (line) => (matches.length ? "" : line.item_id || "");
  (invoice.lines?.length ? invoice.lines : [{}]).forEach((line, index) => addManualDraftLine({
    "Transaction Number": 1,
    Item: matchFor(index)?.item || typedItem(line),
    UPC: matchFor(index)?.gtin || line.gtin || "",
    "Unit Cost": line.price ?? "",
    Quantity: line.qty ?? "",
    "Unit Tax Code": draftValue(job, "taxCode", invoice.taxCode) || "",
    _description: line.description || "",
  }));
  $("#manual-draft-acknowledge").checked = false;
  $("#manual-draft-error").hidden = true;
}

function openManualDraft() {
  const job = app.currentJob;
  if (!job) return;
  if (app.manualDraftJobId !== job.id) {
    app.manualDraftJobId = job.id;
    initializeManualDraft(job);
  }
  $("#manual-draft-source-name").textContent = job.filename;
  const source = $("#manual-draft-source-link");
  if (app.documentJobId === job.id && app.documentUrl) {
    source.href = app.documentUrl;
    source.removeAttribute("aria-disabled");
  } else {
    source.removeAttribute("href");
    source.setAttribute("aria-disabled", "true");
  }
  $("#manual-draft-dialog").showModal();
}

function draftSection(sectionName, columns) {
  const section = $(`[data-draft-section="${sectionName}"]`, $("#manual-draft-form"));
  return Object.fromEntries(columns.map((column) => {
    const input = [...section.elements].find((element) => element.name === column);
    return [column, column === "Transaction Number" ? Number(input.value) : input.value.trim()];
  }));
}

function draftDetails() {
  return $$("tr", $("#manual-draft-lines")).map((row) => Object.fromEntries(DRAFT_DETAIL_COLUMNS.map((column) => {
    const input = [...row.querySelectorAll("input")].find((element) => element.name === column);
    return [column, column === "Transaction Number" ? Number(input.value) : input.value.trim()];
  })));
}

async function downloadManualDraft(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = $("#manual-draft-error");
  error.hidden = true;
  if (!form.checkValidity()) {
    error.textContent = "Complete every required workbook field. The first missing or invalid value is highlighted.";
    error.hidden = false;
    form.reportValidity();
    return;
  }
  const body = {
    acknowledge_unvalidated: $("#manual-draft-acknowledge").checked,
    Header: draftSection("Header", DRAFT_HEADER_COLUMNS),
    Tax_Breakdown: draftSection("Tax_Breakdown", DRAFT_TAX_COLUMNS),
    Details: draftDetails(),
  };
  const net = Number(body.Header["Total Cost Ex Tax"]);
  const basis = Number(body.Tax_Breakdown["Tax Basis"]);
  const detailTotal = body.Details.reduce((sum, row) => sum + Number(row["Unit Cost"]) * Number(row.Quantity), 0);
  if (!Number.isFinite(net) || !Number.isFinite(basis) || !Number.isFinite(detailTotal) || net.toFixed(4) !== basis.toFixed(4) || net.toFixed(4) !== detailTotal.toFixed(4)) {
    error.textContent = "Tax Basis and the detail Unit Cost × Quantity total must both equal Total Cost Ex Tax.";
    error.hidden = false;
    return;
  }

  const button = $("#download-manual-draft");
  button.disabled = true;
  button.textContent = "Preparing draft…";
  try {
    const response = await studioFetch(`/api/jobs/${encodeURIComponent(app.manualDraftJobId)}/draft`, { method: "POST", body });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "Invoice_DRAFT_UNVALIDATED.xlsx"));
    $("#manual-draft-dialog").close();
    notify("Unvalidated draft downloaded. The invoice job and allocations were not changed.");
  } catch (requestError) {
    error.textContent = requestError.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "Download DRAFT_UNVALIDATED.xlsx";
  }
}

function openExtractionDraft() {
  if (!app.currentJob || ["queued", "processing"].includes(app.currentJob.status) || !hasStructuredInvoiceData(app.currentJob)) return;
  $("#extraction-draft-dialog").showModal();
}

async function downloadExtractionDraft() {
  if (!app.currentJob) return;
  const job = app.currentJob;
  const button = $("#confirm-extraction-draft");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioFetch(`/api/jobs/${encodeURIComponent(job.id)}/extraction-draft`, {
      method: "POST",
      body: { revision: job.revision, acknowledge_unvalidated: true },
    });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "Invoice_EXTRACTION_REVIEW_ONLY.xlsx"));
    $("#extraction-draft-dialog").close();
    notify("Extracted review-only workbook downloaded. Approval and receipt allocations were not changed.");
  } catch (error) {
    notify(error.message, "error", 7000);
  } finally {
    button.disabled = false;
    button.textContent = "Download EXTRACTION_REVIEW_ONLY.xlsx";
  }
}

function notify(message, type = "success", duration = 4600) {
  const banner = $("#global-message");
  banner.textContent = message;
  banner.className = `global-message ${type}`;
  banner.hidden = false;
  clearTimeout(app.messageTimer);
  app.messageTimer = setTimeout(() => { banner.hidden = true; }, duration);
}

function formatDate(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function addedTimestamp(job) {
  const time = Date.parse(job.created_at || "");
  return Number.isNaN(time) ? null : time;
}

// The server lists jobs by last update, so a job moves whenever it changes.
// The inbox orders by the stable upload time instead; unknown times go last.
function sortJobsByAdded(jobs, order = app.inboxSort) {
  const direction = order === "oldest" ? 1 : -1;
  return [...jobs].sort((left, right) => {
    const a = addedTimestamp(left);
    const b = addedTimestamp(right);
    if (a === b) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return (a - b) * direction;
  });
}

function formatAddedTime(value, now = new Date()) {
  const parsed = new Date(value || "");
  if (!value || Number.isNaN(parsed.valueOf())) return "";
  if (parsed.toDateString() === now.toDateString()) return parsed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const options = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  if (parsed.getFullYear() !== now.getFullYear()) options.year = "numeric";
  return parsed.toLocaleString([], options);
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(0.1, bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// selected_engine is an engine name, or "<provider> / <model>" for an AI read: the model ID shows as typed, as in the trace.
function engineLabel(value) {
  const [engine, ...model] = String(value || "").split(" / ");
  return `${humanize(engine)}${model.length ? ` · ${model.join(" / ")}` : ""}`;
}

function humanize(value) {
  const names = {
    invoice2data: "invoice2data",
    paddleocr: "PaddleOCR",
    docling: "Docling",
    native_pdf_text: "PDF text",
    openai: "OpenAI",
    anthropic: "Anthropic",
    azure: "Azure AI Foundry",
    chatgpt: "ChatGPT",
    claude_local: "Claude subscription",
    vertex: "Google Cloud AI",
  };
  if (names[value]) return names[value];
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function auditEventLabel(event) {
  const labels = {
    extracted: "Reader run finished (legacy event)",
    text_read: "Text read",
    fields_extracted: "Invoice fields extracted",
    extraction_failed: "Extraction failed",
  };
  return labels[event] || humanize(event);
}

function statusClass(job) {
  if (job.status === "ready" || job.status === "exported") return "success";
  if (job.status === "error") return "error";
  if (job.status === "review" || job.status === "processing" || job.status === "queued") return "warning";
  return "neutral";
}

function isBatchReady(job) {
  return job.status === "ready" && Boolean(job.validation?.ready) && !job.export_id;
}

function isBatchReviewable(job) {
  return ["review", "ready", "exported"].includes(job.status) && hasStructuredInvoiceData(job);
}

function isSelectableInvoice(job) {
  return Boolean(job.id) && !["queued", "processing", "deleting"].includes(job.status);
}

// Decision 64: "Select all" covers the selectable invoices in the list as shown, so a search narrows it.
function visibleSelectableJobs() {
  const visible = new Set(app.visibleJobIds || []);
  return (app.state?.jobs || []).filter((job) => visible.has(job.id) && isSelectableInvoice(job));
}

function navigate(sectionName) {
  $$(".page-section").forEach((section) => {
    const active = section.id === `${sectionName}-section`;
    section.hidden = !active;
    section.classList.toggle("active", active);
  });
  $$('[data-nav]').forEach((button) => {
    const active = button.dataset.nav === sectionName;
    button.classList.toggle("active", active);
    if (button.classList.contains("nav-item")) {
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
  });
  history.replaceState(null, "", `#${sectionName}`);
  $("#main-content").focus({ preventScroll: true });
  if (sectionName === "references") {
    ensureReferenceLookup().catch((error) => notify(error.message, "error"));
    loadMappingTables().catch((error) => notify(error.message, "error"));
  }
  if (sectionName === "engines" && app.state) loadProviderModels("settings");
  if (sectionName === "accuracy") loadAccuracy().catch((error) => notify(error.message, "error"));
  if (sectionName === "users") loadUsers();
}

async function loadState({ preserveSelection = true } = {}) {
  const data = await api("/api/state");
  app.state = data;
  await ensureManagedVertexModel();
  const knownIds = new Set(data.jobs.map((job) => job.id));
  app.selectedForBatch.forEach((id) => { if (!knownIds.has(id)) app.selectedForBatch.delete(id); });
  renderJobs();
  renderReferences();
  renderEngines();
  renderPolicy();
  applySettings();
  renderConnections();
  if (preserveSelection && app.selectedJobId) {
    const summary = data.jobs.find((job) => job.id === app.selectedJobId);
    if (!summary) clearSelectedJob();
  }
  return data;
}

function renderJobs() {
  const query = $("#job-search").value.trim().toLowerCase();
  const jobs = [...(app.state?.jobs || [])];
  const filtered = sortJobsByAdded(jobs.filter((job) => {
    const invoice = job.invoice || {};
    return [job.filename, invoice.supplier_name, invoice.seller, invoice.number].some((value) => String(value || "").toLowerCase().includes(query));
  }));
  $("#job-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#job-empty").hidden = jobs.length > 0;
  $("#job-list").hidden = jobs.length === 0;
  $("#batch-controls").hidden = jobs.length === 0;
  $("#inbox-sort").hidden = jobs.length === 0;
  $$("[data-inbox-sort]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.inboxSort === app.inboxSort)));
  const now = new Date();
  const list = $("#job-list");
  list.replaceChildren();
  app.visibleJobIds = filtered.map((job) => job.id);

  filtered.forEach((job) => {
    const row = make("div", `job-row${job.id === app.selectedJobId ? " active" : ""}`);
    const select = make("input", "job-select");
    select.type = "checkbox";
    select.checked = app.selectedForBatch.has(job.id);
    select.disabled = !isSelectableInvoice(job);
    select.setAttribute("aria-label", `Select ${job.filename} for download or deletion`);
    select.addEventListener("change", () => {
      if (select.checked) app.selectedForBatch.add(job.id);
      else app.selectedForBatch.delete(job.id);
      updateBatchControls();
    });

    const button = make("button", "job-item");
    button.type = "button";
    button.setAttribute("aria-label", `Review ${job.filename}`);
    const extension = (job.filename.split(".").pop() || "DOC").slice(0, 4).toUpperCase();
    button.append(make("span", "job-icon", extension));
    const copy = make("span", "job-copy");
    copy.append(make("strong", "", job.invoice?.supplier_name || job.invoice?.seller || job.filename));
    const secondary = job.invoice?.number ? `${job.invoice.number} · ${STATUS_LABELS[job.status] || humanize(job.status)}` : STATUS_LABELS[job.status] || humanize(job.status);
    const meta = make("span", "job-meta");
    meta.append(make("span", "job-meta-status", secondary));
    const chip = targetChip(job);
    if (chip) meta.append(chip);
    const added = formatAddedTime(job.created_at, now);
    if (added) {
      const time = make("time", "job-time", added);
      time.dateTime = job.created_at;
      time.title = `Added ${formatDate(job.created_at)}`;
      meta.append(time);
    }
    copy.append(meta);
    button.append(copy);
    const dot = make("span", `status-dot ${job.status === "error" ? "error" : ["processing", "queued"].includes(job.status) ? "processing" : ""}`);
    dot.setAttribute("aria-hidden", "true");
    button.append(dot);
    button.addEventListener("click", () => selectJob(job.id, { reveal: true }));
    row.append(select, button);
    list.append(row);
  });
  if (jobs.length && !filtered.length) list.append(make("p", "table-empty", "No invoices match this search."));
  updateBatchControls();
}

function updateBatchControls() {
  const jobs = app.state?.jobs || [];
  const valid = [...app.selectedForBatch].filter((id) => isSelectableInvoice(jobs.find((job) => job.id === id) || {}));
  if (valid.length !== app.selectedForBatch.size) app.selectedForBatch = new Set(valid);
  const selectedJobs = valid.map((id) => jobs.find((job) => job.id === id)).filter(Boolean);
  const approvedSelected = selectedJobs.filter(isBatchReady).length;
  const reviewableSelected = selectedJobs.filter(isBatchReviewable).length;
  $("#selected-count").textContent = `${valid.length} selected`;
  $("#selected-detail").textContent = valid.length
    ? `${reviewableSelected} have extracted data; ${approvedSelected} approved. Delete removes only the selected invoices.`
    : "Select invoices to download together or delete.";
  $("#batch-review").disabled = valid.length === 0 || reviewableSelected !== valid.length;
  $("#batch-fine-rules").disabled = valid.length === 0 || reviewableSelected !== valid.length;
  $("#batch-export").disabled = valid.length === 0 || approvedSelected !== valid.length || app.reviewDirty;
  // Decision 67: the draft needs only one selected invoice with extracted data; the others are left out, never blocking it.
  $("#batch-draft").disabled = reviewableSelected === 0;
  $("#batch-delete").disabled = valid.length === 0;
  $("#select-all-finished").disabled = !jobs.some(isSelectableInvoice);
  const visible = visibleSelectableJobs();
  const ticked = visible.filter((job) => app.selectedForBatch.has(job.id)).length;
  const selectAll = $("#select-all-jobs");
  selectAll.checked = visible.length > 0 && ticked === visible.length;
  selectAll.indeterminate = ticked > 0 && ticked < visible.length;
  selectAll.disabled = visible.length === 0;
  const processed = jobs.filter(isBatchReviewable);
  const allProcessedSelected = processed.length > 0 && processed.every((job) => app.selectedForBatch.has(job.id));
  $("#select-processed").textContent = allProcessedSelected ? "Clear selection" : `Select processed${processed.length ? ` (${processed.length})` : ""}`;
  $("#select-processed").disabled = processed.length === 0;
  const ready = jobs.filter(isBatchReady);
  const allSelected = ready.length > 0 && ready.every((job) => app.selectedForBatch.has(job.id));
  $("#select-ready").textContent = allSelected && valid.length === ready.length ? "Clear selection" : `Select approved${ready.length ? ` (${ready.length})` : ""}`;
  $("#select-ready").disabled = ready.length === 0;
  renderBatchWorkflow(jobs);
}

function renderBatchWorkflow(jobs) {
  const active = jobs.filter((job) => ["queued", "processing"].includes(job.status)).length;
  const processed = jobs.filter(isBatchReviewable).length;
  $("#workflow-upload-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#workflow-process-count").textContent = `${active} active`;
  $("#workflow-review-count").textContent = `${processed} processed`;
  $("#workflow-download-count").textContent = `${app.selectedForBatch.size} selected`;
}

// A reviewer's tap reveals the review when it sits off-screen (stacked layouts), and another invoice starts at the
// top of its details instead of the previous invoice's scroll position.
async function selectJob(id, { reveal = false } = {}) {
  if (id !== app.selectedJobId && app.reviewDirty) {
    notify("Save this invoice before opening another one. Your edits are still here.", "error", 7000);
    return;
  }
  const token = ++app.selectionToken;
  // A clean form that the reviewer edits while this job's GET is in flight keeps the edits: re-rendering would
  // silently replace them before the save. A form already dirty before the await (the save-conflict reload) still
  // loads the server version.
  const dirtyBefore = app.reviewDirty;
  app.selectedJobId = id;
  renderJobs();
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (token !== app.selectionToken) return;
    if (!dirtyBefore && app.reviewDirty && app.currentJob?.id === job.id) return;
    const switched = app.currentJob?.id !== job.id;
    app.currentJob = job;
    app.reviewDirty = false;
    renderSelectedJob(job);
    if (switched) $(".fields-card").scrollTop = 0;
    if (reveal) revealReview();
    startPollingIfNeeded();
  } catch (error) {
    notify(error.message, "error");
  }
}

function revealReview() {
  const panel = $("#review-panel");
  if (panel.hidden) return;
  const top = panel.getBoundingClientRect().top;
  const stacked = top >= $(".inbox-card").getBoundingClientRect().bottom - 1;
  if (stacked && (top < 0 || top > window.innerHeight * 0.25)) panel.scrollIntoView({ block: "start" });
}

function clearSelectedJob() {
  app.selectionToken += 1;
  app.selectedJobId = null;
  app.currentJob = null;
  app.reviewDirty = false;
  $("#workspace-empty").hidden = false;
  $("#review-panel").hidden = true;
  if (app.documentUrl) URL.revokeObjectURL(app.documentUrl);
  app.documentUrl = null;
  app.documentJobId = null;
  app.documentToken += 1;
  $("#document-frame").removeAttribute("src");
  $("#document-image").removeAttribute("src");
}

function plannedProcessingStages(job) {
  const options = job.options || {};
  const current = job.progress?.engine;
  let stages;
  if (options.engine === "auto" || !options.engine) stages = ["invoice2data", "paddleocr", "docling"];
  else if (options.engine === "ai") stages = [];
  // Like engines.process(): an OCR reader chosen for a PDF checks the embedded text first.
  else if (["paddleocr", "docling"].includes(options.engine) && /\.pdf$/i.test(job.filename || "") && options.prefer_native_text !== false) {
    stages = ["native_pdf_text", options.engine];
  } else stages = [options.engine];

  if (options.engine === "ai" || options.ai_fallback) stages.push(options.provider || "openai");
  if (current && !stages.includes(current)) {
    const aiIndex = stages.findIndex((stage) => AI_ENGINES.includes(stage));
    stages.splice(aiIndex < 0 ? stages.length : aiIndex, 0, current);
  }
  return [...new Set(stages.filter(Boolean))];
}

function renderProcessingStages(job) {
  const list = $("#processing-stages");
  list.replaceChildren();
  const stages = plannedProcessingStages(job);
  const current = job.status === "processing" ? job.progress?.engine : null;
  const currentIndex = current ? stages.indexOf(current) : -1;
  stages.forEach((engine, index) => {
    const isAI = AI_ENGINES.includes(engine);
    let state = "next";
    let stateLabel = index === 0 && job.status === "queued" ? "Starts first" : "Next if needed";
    if (index === currentIndex) {
      state = "active";
      stateLabel = job.progress?.message ? `Working now · ${job.progress.message}` : "Working now";
    } else if (currentIndex > index) {
      state = "complete";
      stateLabel = "Checked";
    }
    if (isAI && !String(job.options?.model || "").trim()) stateLabel = "Needs a model to run";
    const item = make("li", `processing-stage ${state}`);
    const marker = make("span", "stage-marker", state === "complete" ? "✓" : String(index + 1));
    marker.setAttribute("aria-hidden", "true");
    const copy = make("span", "stage-copy");
    const model = isAI && job.options?.model ? ` · ${job.options.model}` : "";
    copy.append(make("strong", "", `${humanize(engine)}${model}`), make("small", "", stateLabel));
    item.append(marker, copy);
    list.append(item);
  });
}

function hasStructuredInvoiceData(job) {
  const invoice = job.invoice || {};
  return Boolean(invoice.lines?.length) || HEADER_FIELDS.some(([name]) => {
    const value = invoice[name];
    return value !== null && value !== undefined && String(value).trim() !== "";
  });
}

// A coverage figure beside an invoice with no item lines or no key fields reads like a pass (D88), so the box says
// "Not read" and gives the reason the reader trace shows instead of a percentage.
const KEY_FIELDS = ["number", "po", "date", "net", "tax"];
const AI_ENGINES = ["openai", "anthropic", "azure", "chatgpt", "claude_local", "vertex"];

function readFailure(job) {
  const invoice = job.invoice || {};
  const lines = invoice.lines?.length || 0;
  const keyFields = KEY_FIELDS.filter((name) => String(invoice[name] ?? "").trim() !== "").length;
  if (lines && keyFields) return null;
  const what = !lines && !keyFields ? "no item lines and no key fields were found" : !lines ? "no item lines were found" : "no key fields were found";
  return { what, why: readFailureReasons(job).join("; ") || "no reader returned them" };
}

// What lowers the figure, in the order engines.quality() counts it, so a missing tax total is not read as missing items.
function missingFields(invoice) {
  const blank = (value) => value === null || value === undefined || String(value).trim() === "";
  const labels = { number: "Invoice number", date: "Invoice date", currency: "Currency", net: "Net total", tax: "Tax total" };
  const missing = Object.entries(labels).filter(([name]) => blank(invoice[name])).map(([, label]) => `${label} missing`);
  if (!blank(invoice.date) && !/^\d{4}-\d{2}-\d{2}$/.test(String(invoice.date))) missing.push("Invoice date not a calendar date");
  const lines = invoice.lines || [];
  [["qty", "quantity"], ["price", "unit price"], ["uom", "unit of measure"]].forEach(([name, label]) => {
    const count = lines.filter((line) => blank(line[name])).length;
    if (count) missing.push(`${count} line${count === 1 ? "" : "s"} without ${label}`);
  });
  const unidentified = lines.filter((line) => blank(line.sku) && blank(line.gtin)).length;
  if (unidentified) missing.push(`${unidentified} line${unidentified === 1 ? "" : "s"} without item id`);
  if (lines.length && !blank(invoice.net) && lines.every((line) => !blank(line.qty) && !blank(line.price))) {
    const total = lines.every((line) => !blank(line.net_amount))
      ? lines.reduce((sum, line) => sum + Number(line.net_amount), 0)
      : lines.reduce((sum, line) => sum + Number(line.qty) * Number(line.price), 0);
    // The server compares Decimals; rounding the float gap drops summing noise (0.1 + 0.2 - 0.29 is not over 0.01).
    if (Math.round(Math.abs(total - Number(invoice.net)) * 1e6) / 1e6 > 0.01) missing.push("line amounts do not add up to the net total");
  }
  return missing;
}

function readFailureReasons(job) {
  const trace = job.trace || [];
  const reasons = [];
  const textCharacters = Math.max(String(job.text || "").trim().length, ...trace.map((entry) => Number(entry.text_characters) || 0));
  const pageDocument = /\.(pdf|png|jpe?g|tiff?|webp|bmp|gif|heic)$/i.test(job.filename || "");
  if (trace.some((entry) => entry.status === "needs_ocr") || (pageDocument && textCharacters < 20)) reasons.push("scanned image, no text layer");
  const ocr = trace.filter((entry) => ["paddleocr", "docling"].includes(entry.engine));
  if (ocr.some((entry) => entry.status === "failed")) reasons.push("OCR failed");
  // A reader whose models the network would not let it download is installed and fine: saying "OCR
  // failed" or "not installed" sends someone to reinstall it, which cannot help.
  else if (ocr.length && ocr.every((entry) => ["unavailable", "models_unavailable"].includes(entry.status))) {
    reasons.push(ocr.some((entry) => entry.status === "models_unavailable")
      ? "the OCR reader could not download its models (the network refused it)"
      : "OCR reader not installed");
  }
  else if (ocr.some((entry) => ["text_only", "no_fields"].includes(entry.status))) reasons.push("OCR read no invoice fields");
  else if (ocr.some((entry) => entry.status === "extracted" && !Number(entry.line_items))) reasons.push("OCR read no item lines");
  const ai = trace.filter((entry) => AI_ENGINES.includes(entry.engine) || entry.model !== undefined);
  const aiStatus = (status) => ai.some((entry) => entry.status === status);
  if (!ai.length) reasons.push("AI fallback not run");
  else if (aiStatus("failed")) reasons.push("AI read failed");
  else if (aiStatus("needs_connection")) reasons.push("AI fallback not set up");
  else if (aiStatus("no_fields")) reasons.push("AI returned no fields");
  else if (ai.some((entry) => entry.status === "extracted" && !Number(entry.line_items))) reasons.push("AI returned no item lines");
  else if (aiStatus("skipped")) reasons.push("AI fallback skipped");
  return reasons;
}

function renderSelectedJob(job) {
  $("#workspace-empty").hidden = true;
  $("#review-panel").hidden = false;
  $("#selected-filename").textContent = job.filename;
  $("#selected-file-icon").textContent = (job.filename.split(".").pop() || "DOC").slice(0, 4).toUpperCase();
  const engine = job.selected_engine && job.selected_engine !== "pending" ? engineLabel(job.selected_engine) : "Engine pending";
  $("#selected-meta").textContent = `${engine}${job.created_at ? ` · Added ${formatDate(job.created_at)}` : ""}`;
  const processing = ["queued", "processing"].includes(job.status);
  const emptyExtraction = !processing && !hasStructuredInvoiceData(job);
  const status = $("#selected-status");
  status.textContent = emptyExtraction ? "Needs review" : (STATUS_LABELS[job.status] || humanize(job.status));
  status.className = `status-pill ${emptyExtraction ? "warning" : statusClass(job)}`;
  $("#retry-job").disabled = ["queued", "processing", "exported"].includes(job.status);

  $("#processing-banner").hidden = !processing;
  $("#processing-result").hidden = !processing;
  $("#empty-extraction").hidden = !emptyExtraction;
  $("#invoice-form").hidden = processing;
  $(".review-controls").hidden = processing;
  $("#completeness").hidden = processing || emptyExtraction;
  $(".trace-card").hidden = processing;
  $("#empty-retry").disabled = $("#retry-job").disabled;
  const hasText = Boolean(String(job.text || "").trim());
  $("#empty-show-text").hidden = !hasText;
  const failure = processing ? null : readFailure(job);
  $("#empty-extraction-title").textContent = hasText ? "Text read, invoice fields not extracted" : "This document could not be read";
  $("#empty-extraction-detail").textContent = (failure ? `Why: ${failure.why}. ` : "") + (hasText
    ? "The document text is available, but Invoice Studio could not turn it into invoice fields. Nothing is approved and export remains on hold."
    : "No structured invoice fields are available yet. Retry with another extraction path, configure AI fallback or enter the workbook values manually.");
  if (processing) {
    const title = job.status === "queued" ? "Waiting for an extraction slot…" : `${humanize(job.progress?.engine || "engine")} is reading the invoice…`;
    const detail = job.progress?.message || "The result will appear here when it is ready.";
    $("#processing-title").textContent = title;
    $("#processing-detail").textContent = detail;
    $("#processing-result-title").textContent = title;
    $("#processing-result-detail").textContent = detail;
    renderProcessingStages(job);
  }
  const possiblePurchaseOrder = job.document_type_hint === "possible_purchase_order";
  const extractionNote = String(job.extraction_note || (possiblePurchaseOrder
    ? "The source appears to be a purchase order rather than an invoice. Check the document and enter a draft manually if you still need a workbook."
    : "")).trim();
  $("#extraction-note").hidden = processing || !extractionNote;
  if (extractionNote) {
    $("#extraction-note-title").textContent = possiblePurchaseOrder ? "This may be a purchase order"
      : job.split ? `Invoice ${job.split.part} of ${job.split.of} from one uploaded file` : "Extraction needs review";
    $("#extraction-note-detail").textContent = extractionNote;
  }
  $("#job-error").hidden = !job.error;
  $("#job-error").textContent = job.error || "";

  renderDocument(job).catch((error) => notify(error.message, "error"));
  if (processing) {
    renderValidation(job);
    return;
  }
  renderInvoiceForm(job);
  renderValidation(job);
  renderTargetCheck(job);
  renderTrace(job);
  renderReviewActions(job);
}

async function renderDocument(job) {
  const text = String(job.text || "").trim();
  $("#extracted-text").textContent = text;
  $("#extracted-text-wrap").hidden = !text;
  if (app.documentJobId === job.id) return;

  const token = ++app.documentToken;
  const endpoint = `/api/jobs/${encodeURIComponent(job.id)}/document`;
  const ext = (job.filename.split(".").pop() || "").toLowerCase();
  const isImage = ["png", "jpg", "jpeg", "webp", "bmp"].includes(ext);
  const canPreview = ext === "pdf" || isImage;
  app.documentJobId = job.id;
  if (app.documentUrl) URL.revokeObjectURL(app.documentUrl);
  app.documentUrl = null;
  $("#open-document").removeAttribute("href");
  $("#download-original").removeAttribute("href");
  $("#document-frame").hidden = ext !== "pdf";
  $("#document-image").hidden = !isImage;
  $("#document-download-card").hidden = canPreview;
  $("#document-frame").removeAttribute("src");
  $("#document-image").removeAttribute("src");
  try {
    const { blob } = await apiBlob(endpoint);
    if (token !== app.documentToken || app.currentJob?.id !== job.id) return;
    app.documentUrl = URL.createObjectURL(blob);
    $("#open-document").href = app.documentUrl;
    $("#download-original").href = app.documentUrl;
    if (ext === "pdf") $("#document-frame").src = app.documentUrl;
    if (isImage) $("#document-image").src = app.documentUrl;
  } catch (error) {
    if (token === app.documentToken && app.documentJobId === job.id) app.documentJobId = null;
    throw error;
  }
}

function renderInvoiceForm(job) {
  const invoice = job.invoice || {};
  const fields = $("#header-fields");
  fields.replaceChildren();
  HEADER_FIELDS.forEach(([name, label, type, options = {}]) => {
    if (rulesJob(job) && RULES_DERIVED_FIELDS.has(name)) return;
    const wrapper = make("label", "field-wrap");
    wrapper.append(make("span", "", label));
    const input = make("input");
    input.name = name;
    input.type = type;
    input.readOnly = Boolean(options.readOnly);
    if (input.readOnly) input.setAttribute("aria-readonly", "true");
    if (type === "number") input.step = "any";
    if (name === "currency") { input.maxLength = 3; input.autocapitalize = "characters"; }
    input.value = oneLine(invoice[name]);
    const buyer = name === "buyer_name" ? rulesBuyer(job) : null;
    if (buyer) {
      input.value = oneLine(buyer.value);
      input.dataset.rulesValue = input.value;
    }
    input.disabled = ["queued", "processing", "exported"].includes(job.status);
    wrapper.append(input);
    const badge = readerBadge(job.readers?.header?.[name]);
    if (badge) wrapper.append(badge);
    const review = noteFor(input, reviewNote(job.evidence?.header?.[name]?.review));
    if (review) wrapper.append(review);
    if (buyer) {
      wrapper.append(make("small", "buyer-evidence", buyerEvidence(buyer)));
      const printed = String(invoice.buyer_name ?? "").trim();
      if (printed && foldText(printed) !== foldText(buyer.value)) wrapper.append(make("small", "buyer-printed", `printed: ${printed}`));
    }
    const order = name === "po" ? pogrnOrder(job) : null;
    if (order) wrapper.append(make("small", "po-pogrn", `Order No ${order.value} · ${order.source}`));
    const orderDate = name === "po" ? orderDateNote(job) : null;
    if (orderDate) {
      const note = make("small", "po-order-date", orderDate.text);
      if (orderDate.title) note.title = orderDate.title;
      wrapper.append(note);
    }
    if (name === "date_printed") {
      wrapper.append(make("small", "", invoice.date_printed
        ? "Read-only source evidence. It is never converted automatically."
        : "No separate printed source date was captured."));
    }
    if (name === "date" && invoice.date_printed && !invoice.date) {
      wrapper.append(make("small", "", "The printed value may be day/month ambiguous. Choose the intended calendar date before approval."));
    }
    fields.append(wrapper);
  });
  const lines = $("#line-items");
  lines.replaceChildren();
  const reviews = lineReviews(job);
  const readers = lineReaders(job);
  (invoice.lines || []).forEach((line, n) => addLine(line, ["queued", "processing", "exported"].includes(job.status), reviews?.[n], readers?.[n]));
  let hiddenReviews = $("#line-reviews-hidden");
  if (!hiddenReviews) {
    hiddenReviews = make("p", "rules-flagged", "Line read notes hidden after a line was added or removed.");
    hiddenReviews.id = "line-reviews-hidden";
    $("#no-lines").before(hiddenReviews);
  }
  hiddenReviews.hidden = Boolean(reviews) || !hasLineReview(job);
  $("#no-lines").hidden = Boolean(invoice.lines?.length);
  $("#add-line").disabled = ["queued", "processing", "exported"].includes(job.status);
  renderAiReader(job);
  renderRulesResult(job);
  const raw = Number(job.completeness);
  const percent = Number.isFinite(raw) ? Math.round(Math.max(0, Math.min(1, raw)) * 100) : null;
  const failure = readFailure(job);
  const lowered = failure || percent === null || percent >= 100 ? [] : missingFields(invoice);
  $("#completeness").classList.toggle("not-read", Boolean(failure));
  $("#completeness strong").textContent = failure ? "Not read" : percent === null ? "—" : `${percent}%`;
  $("#completeness-reason").hidden = !failure && !lowered.length;
  $("#completeness-reason").textContent = failure
    ? `Could not read this document: ${failure.what} (${failure.why}).`
    : lowered.length ? `Lowered by: ${lowered.join("; ")}.` : "";
  app.reviewDirty = false;
  renderReviewSaveState();
}

function renderReviewSaveState() {
  const state = $("#review-save-state");
  state.textContent = app.reviewDirty
    ? "Unsaved changes — save before batch download or opening another invoice."
    : "Saved";
  state.classList.toggle("unsaved", app.reviewDirty);
  updateBatchControls();
}

function markReviewDirty() {
  if (!app.currentJob || ["queued", "processing", "exported"].includes(app.currentJob.status)) return;
  app.reviewDirty = true;
  renderReviewSaveState();
}

// A text input drops line breaks from its value, fusing the words either side; show (and so save) each break as a space.
const oneLine = (value) => String(value ?? "").replace(/\r\n?|\n/g, " ");

function lineInput(name, value, label, type = "text") {
  const input = make("input");
  input.name = name;
  input.value = oneLine(value);
  input.type = type;
  if (type === "number") input.step = "any";
  input.setAttribute("aria-label", label);
  return input;
}

// An input followed by its reader and its read review, if the reader left one for that line field.
const withReview = (input, reviews, readers) => [input, readerBadge(readers?.[input.name]),
  noteFor(input, reviewNote(reviews?.[input.name]?.review))].filter(Boolean);

function printedLineAmounts(line, reviews, readers) {
  const details = make("details", "line-amounts");
  const summary = make("summary");
  const net = lineInput("net_amount", line.net_amount, "Printed line net", "number");
  const tax = lineInput("tax_amount", line.tax_amount, "Printed line tax", "number");
  const update = () => {
    const values = [net.value.trim() ? `Net ${net.value.trim()}` : "", tax.value.trim() ? `Tax ${tax.value.trim()}` : ""].filter(Boolean);
    summary.textContent = values.length ? values.join(" · ") : "Add printed line net / tax";
  };
  [["Printed line net", net], ["Printed line tax", tax]].forEach(([labelText, input]) => {
    const label = make("label");
    label.append(make("span", "", labelText), ...withReview(input, reviews, readers));
    details.append(label);
    input.addEventListener("input", update);
  });
  details.prepend(summary);
  details.open = line.net_amount !== null && line.net_amount !== undefined
    || line.tax_amount !== null && line.tax_amount !== undefined
    || Boolean(reviews?.net_amount?.review?.reason || reviews?.tax_amount?.review?.reason);
  update();
  return details;
}

function addLine(line = {}, disabled = false, reviews = null, readers = null) {
  const row = make("tr");
  // The printed part code has no input; it rides on the row so a save keeps it (the review replaces the lines).
  if (line.part_code) row.dataset.partCode = line.part_code;
  const identity = make("td");
  identity.append(...withReview(lineInput("item_id", line.item_id, "Internal item · confirmed reference"), reviews, readers),
    ...withReview(lineInput("sku", line.sku, "Supplier SKU"), reviews, readers), ...withReview(lineInput("gtin", line.gtin, "GTIN or barcode"), reviews, readers));
  identity.querySelector('[name="item_id"]').placeholder = "Internal item · reference";
  identity.querySelector('[name="sku"]').placeholder = "Supplier SKU";
  identity.querySelector('[name="gtin"]').placeholder = "Barcode / GTIN";
  const description = make("td");
  description.append(...withReview(lineInput("description", line.description, "Description"), reviews, readers));
  const find = make("button", "line-lookup", "Find item by description");
  find.type = "button";
  find.disabled = disabled;
  find.addEventListener("click", () => beginLineReferenceLookup("review", row));
  description.append(find);
  const qty = make("td"); qty.append(...withReview(lineInput("qty", line.qty, "Quantity", "number"), reviews, readers));
  const uom = make("td"); uom.append(...withReview(lineInput("uom", line.uom, "Unit of measure"), reviews, readers));
  const price = make("td"); price.append(...withReview(lineInput("price", line.price, "Unit price", "number"), reviews, readers), printedLineAmounts(line, reviews, readers));
  const evidence = make("td");
  evidence.append(lineInput("evidence", line.evidence, "Evidence text"), lineInput("page", line.page, "Evidence page", "number"));
  const action = make("td");
  const remove = make("button", "remove-line", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove line item");
  remove.addEventListener("click", () => { row.remove(); $("#no-lines").hidden = Boolean($("#line-items").children.length); markReviewDirty(); });
  action.append(remove);
  row.append(identity, description, qty, uom, price, evidence, action);
  $$("input", row).forEach((input) => { input.disabled = disabled; });
  remove.disabled = disabled;
  $("#line-items").append(row);
  $("#no-lines").hidden = true;
}

function renderValidation(job) {
  const issues = job.validation?.issues || [];
  const summary = $("#validation-summary");
  summary.replaceChildren();
  if (["queued", "processing"].includes(job.status)) {
    summary.hidden = true;
    return;
  }
  if (!issues.length) {
    if (job.status === "ready" || job.status === "exported") {
      summary.hidden = false;
      summary.className = "validation-summary";
      summary.append(make("strong", "", "Validation passed"), make("span", "", "Reference checks, totals and evidence review are complete."));
    } else summary.hidden = true;
    return;
  }
  summary.hidden = false;
  // After the reviewer's confirm, what remains are the notes they accepted, not open issues.
  const confirmed = job.status === "ready" || job.status === "exported";
  // One plain verdict first, then the issues in three groups: must fix (holds the invoice whatever the reviewer does),
  // check then confirm (the reviewer's confirm accepts them) and warnings (for information, never holding it).
  const confirmStep = issues.some((issue) => issue.code === "REVIEW" && issue.blocking !== false);
  const listed = issues.filter((issue) => issue.code !== "REVIEW");
  const groups = [
    ["Must fix", listed.filter((issue) => issue.blocking !== false && !issue.accepted && issue.level !== "review" && issue.level !== "warning")],
    ["Check, then confirm review", listed.filter((issue) => issue.blocking !== false && !issue.accepted && issue.level === "review")],
    ["Warnings, for information", listed.filter((issue) => issue.blocking === false && !issue.accepted)],
    ["Accepted at review", listed.filter((issue) => issue.accepted)],
  ];
  const [mustFix, toCheck, warnings] = groups.map(([, items]) => items.length);
  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  let verdict;
  let tone;
  if (confirmed) {
    verdict = listed.length ? `Confirmed at review · ${count(listed.length, "note")} accepted` : "Confirmed at review";
    tone = "accepted";
  } else if (mustFix) {
    verdict = `Cannot approve yet · ${count(mustFix, "issue")} must be fixed`;
    tone = "error";
  } else if (toCheck) {
    verdict = `Check ${count(toCheck, "item")}, then confirm review`;
    tone = "";
  } else {
    verdict = confirmStep ? "Nothing blocks this invoice · confirm review to approve" : "Nothing blocks this invoice";
    tone = "accepted";
  }
  summary.className = `validation-summary${tone ? ` ${tone}` : ""}`;
  const details = make("details", "validation-details");
  const heading = make("summary");
  const title = make("strong", "", verdict);
  if (!confirmed && warnings) title.append(make("small", "", `${count(warnings, "warning")} to read · they do not block approval`));
  heading.append(title, make("span", "", listed.length ? "Show all" : ""));
  details.append(heading);
  groups.forEach(([title, items]) => {
    if (!items.length) return;
    const list = make("ul");
    items.forEach((issue) => {
      const line = issue.line ? `Line ${issue.line}: ` : "";
      const owner = issue.owner ? ` · ${issue.owner}` : "";
      // Owner's Failure Status first, then its checklist id and the engine's exception type when they differ.
      const detail = [issue.check, issue.type && issue.type !== issue.code ? issue.type : ""].filter(Boolean).join(" · ");
      const status = rulesJob(job) ? `${issue.code}${detail ? ` [${detail}]` : ""}: ` : "";
      const warning = issue.accepted ? " (accepted at review)" : issue.blocking === false ? " (warning)" : "";
      list.append(make("li", "", `${line}${status}${issue.message}${owner}${warning}`));
    });
    details.append(make("h4", "validation-group", `${title} (${items.length})`), list);
  });
  if (confirmStep && !confirmed) details.append(make("p", "validation-extra", "Last step: compare the values with the document, then confirm review."));
  summary.append(details);
}

function renderTrace(job) {
  const trace = job.trace || [];
  const traceList = $("#trace-list");
  traceList.replaceChildren();
  if (!trace.length) traceList.append(make("li", "", "No extraction attempts recorded yet."));
  trace.forEach((entry) => {
    if (typeof entry === "string") traceList.append(make("li", "", entry));
    else {
      const engine = humanize(entry.engine || "engine");
      const model = entry.model ? ` · ${entry.model}` : "";
      const status = humanize(entry.status || "attempted");
      const methodNames = {
        template: "Supplier template",
        layout: "Layout conversion",
        docling_table: "Table and word geometry conversion",
        docling_table_higher_resolution: "Table conversion after a higher-resolution read",
        table_higher_resolution: "Table conversion after a higher-resolution read",
        layout_higher_resolution: "Layout conversion after a higher-resolution read",
        text_only: "Text only",
      };
      const method = entry.method ? ` · ${methodNames[entry.method] || humanize(entry.method)}` : "";
      const seconds = Number(entry.seconds);
      const queued = Number(entry.queue_seconds);
      const timing = (Number.isFinite(seconds) ? ` · ${seconds.toFixed(2)} s reading` : "")
        + (Number.isFinite(queued) && queued > 0.05 ? ` · ${queued.toFixed(2)} s waiting for a reader` : "");
      const evidence = [];
      const textCharacters = Number(entry.text_characters);
      if (Number.isFinite(textCharacters)) evidence.push(`${textCharacters.toLocaleString()} text characters`);
      const extractedFields = Number(entry.extracted_fields);
      if (Number.isFinite(extractedFields)) evidence.push(`${extractedFields.toLocaleString()} field${extractedFields === 1 ? "" : "s"}`);
      const lineItems = Number(entry.line_items);
      if (Number.isFinite(lineItems)) evidence.push(`${lineItems.toLocaleString()} item${lineItems === 1 ? "" : "s"}`);
      else if (entry.status === "text_only" || entry.method === "text_only") evidence.push("0 items");
      const evidenceText = evidence.length ? ` · ${evidence.join(" · ")}` : "";
      const nothingRead = extractedFields === 0 || lineItems === 0;
      const completeness = entry.completeness === undefined || nothingRead ? "" : ` · ${Math.round(Number(entry.completeness) * 100)}% fields found`;
      const reason = entry.reason ? ` — ${entry.reason}` : "";
      const recovery = entry.recovery?.attempted
        ? entry.recovery.selected_pass === "higher_resolution" ? " · Recovery read retained after consistency checks" : " · Original read retained; recovery did not safely improve it"
        : "";
      traceList.append(make("li", "", `${engine}${model}: ${status}${method}${timing}${evidenceText}${completeness}${recovery}${reason}`));
    }
  });
  const chosen = trace.findLast?.((entry) => entry && typeof entry === "object" && entry.status === "extracted") || trace.find((entry) => entry && typeof entry === "object" && entry.status === "extracted");
  $("#trace-summary").textContent = job.selected_engine && job.selected_engine !== "none"
    ? `Chosen: ${engineLabel(job.selected_engine)}`
    : chosen ? `Fields returned by ${humanize(chosen.engine)}${chosen.model ? ` · ${chosen.model}` : ""}` : "How this result was produced";

  const provenance = $("#provenance-list");
  provenance.replaceChildren();
  const entries = rulesJob(job) && job.rules
    ? Object.entries(job.rules.fields || {}).filter(([, f]) => f.value !== null && f.value !== undefined).map(([field, f]) => ({ field, source: evidenceText(f.evidence), value: f.value }))
    : Array.isArray(job.provenance) ? job.provenance : Object.entries(job.provenance || {}).map(([field, source]) => ({ field, source }));
  if (!entries.length) {
    provenance.append(make("dt", "", "None"), make("dd", "", "No reference-derived fields recorded."));
  } else entries.forEach((entry) => {
    provenance.append(make("dt", "", humanize(entry.field)), make("dd", "", `${entry.source || "Extracted"}${entry.value ? ` → ${entry.value}` : ""}`));
  });
}

function renderReviewActions(job) {
  const busy = ["queued", "processing"].includes(job.status);
  const exported = job.status === "exported" || Boolean(job.export_id);
  $("#confirm-review").checked = Boolean(job.reviewed);
  $("#confirm-review").disabled = busy || exported;
  $("#save-review").disabled = busy || exported || job.status === "error";
  $("#export-extraction-draft").hidden = busy || !hasStructuredInvoiceData(job);
  $("#export-extraction-draft").disabled = busy;
  $("#export-job").disabled = !job.validation?.ready && !exported;
  $("#export-job").textContent = exported ? "Download Excel" : "Export Excel";
}

function collectInvoice() {
  const invoice = {};
  HEADER_FIELDS.forEach(([name, , type]) => {
    const input = $(`#header-fields [name="${name}"]`);
    // Rules jobs show derived fields (seller, site, ...) in the rules panel instead; keep the stored value.
    if (!input) {
      invoice[name] = app.currentJob?.invoice?.[name] ?? null;
      return;
    }
    const value = input.value.trim();
    // A rules value the reviewer did not edit is display only: the stored reader value is sent back unchanged.
    if (input.dataset.rulesValue !== undefined && value === input.dataset.rulesValue.trim()) {
      invoice[name] = app.currentJob?.invoice?.[name] ?? null;
      return;
    }
    invoice[name] = value === "" ? null : value;
    if (type === "number" && value !== "") invoice[name] = value;
  });
  invoice.lines = $$("#line-items tr").map((row) => {
    const value = (name) => {
      const raw = $(`[name="${name}"]`, row).value.trim();
      return raw === "" ? null : raw;
    };
    const page = value("page");
    return { item_id: value("item_id"), sku: value("sku"), gtin: value("gtin"), description: value("description"), qty: value("qty"), uom: value("uom"), price: value("price"), net_amount: value("net_amount"), tax_amount: value("tax_amount"), evidence: value("evidence"), page: page === null ? null : Number(page), ...(row.dataset.partCode ? { part_code: row.dataset.partCode } : {}) };
  });
  return invoice;
}

// Saves the form; extra fields (a supplier-code pick) ride on the same request. Resolves true once saved.
async function submitReview(extra = {}, done = "") {
  if (!app.currentJob) return false;
  const button = $("#save-review");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/review`, {
      method: "POST",
      body: {
        invoice: collectInvoice(),
        revision: app.currentJob.revision,
        confirm: $("#confirm-review").checked,
        ...(rulesJob(app.currentJob) ? { entries: collectEntries() } : {}),
        ...extra,
      },
    });
    app.currentJob = job;
    replaceJobSummary(job);
    renderSelectedJob(job);
    if (done) notify(done, "success");
    else notify(job.validation?.ready ? "Review confirmed. This invoice is ready to export." : "Saved and revalidated. Resolve the listed issues before export.", job.validation?.ready ? "success" : "error");
    return true;
  } catch (error) {
    notify(error.message, "error", 7000);
    if (error.message.includes("changed")) await selectJob(app.currentJob.id);
    return false;
  } finally {
    button.textContent = "Save & revalidate";
    button.disabled = false;
  }
}

// New uploads land at the top (newest first) or the bottom (oldest first).
function revealNewestJobs() {
  const list = $("#job-list");
  list.scrollTop = app.inboxSort === "oldest" ? list.scrollHeight : 0;
}

function replaceJobSummary(job) {
  const index = app.state.jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) app.state.jobs[index] = { ...app.state.jobs[index], ...job };
  else app.state.jobs.unshift(job);
  renderJobs();
}

async function exportCurrentJob() {
  if (!app.currentJob) return;
  const button = $("#export-job");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/export`, { method: "POST", body: { revision: app.currentJob.revision } });
    await downloadApi(result.url, `Merch_Inv_${result.id.slice(0, 8)}.xlsx`);
    await loadState();
    await selectJob(app.currentJob.id);
    notify("Excel workbook prepared.");
  } catch (error) {
    notify(error.message, "error", 7000);
  } finally {
    button.textContent = "Export Excel";
    button.disabled = false;
  }
}

function selectedBatchReviewJobs() {
  return [...app.selectedForBatch]
    .map((id) => app.state.jobs.find((job) => job.id === id))
    .filter(isBatchReviewable);
}

function openBatchReview() {
  const jobs = selectedBatchReviewJobs();
  if (!jobs.length) return;
  if (app.reviewDirty) {
    notify("Save the current invoice before downloading a batch. Your unsaved edits are still in the form.", "error", 8000);
    $("#save-review").focus();
    return;
  }
  $("#batch-review-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#batch-review-dialog").showModal();
}

function openDeleteInvoices() {
  const jobs = [...app.selectedForBatch].map((id) => app.state.jobs.find((job) => job.id === id)).filter(isSelectableInvoice);
  if (!jobs.length) return;
  app.pendingDelete = jobs.map((job) => ({ id: job.id, revision: job.revision }));
  $("#delete-invoice-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#delete-invoice-list").replaceChildren(...jobs.map((job) => make("li", "", job.filename)));
  $("#delete-invoice-acknowledge").checked = false;
  $("#confirm-delete-invoices").disabled = true;
  $("#delete-invoice-error").hidden = true;
  $("#delete-invoices-dialog").showModal();
}

async function deleteInvoices() {
  if (!$("#delete-invoice-acknowledge").checked || !app.pendingDelete?.length) return;
  const button = $("#confirm-delete-invoices");
  button.disabled = true;
  button.textContent = "Deleting…";
  try {
    const result = await api("/api/jobs/delete", { method: "POST", body: { jobs: app.pendingDelete, confirm_permanent: true } });
    const ids = new Set(result.deleted_ids);
    app.state.jobs = app.state.jobs.filter((job) => !ids.has(job.id));
    ids.forEach((id) => app.selectedForBatch.delete(id));
    if (ids.has(app.selectedJobId)) clearSelectedJob();
    app.pendingDelete = null;
    $("#delete-invoices-dialog").close();
    renderJobs();
    notify(`${result.count} invoice${result.count === 1 ? "" : "s"} permanently removed from this workspace.`, "success", 7000);
  } catch (error) {
    $("#delete-invoice-error").textContent = error.message;
    $("#delete-invoice-error").hidden = false;
  } finally {
    button.textContent = "Delete permanently";
    button.disabled = !$("#delete-invoice-acknowledge").checked;
  }
}

async function downloadBatchReview() {
  const jobs = selectedBatchReviewJobs();
  if (!jobs.length || app.reviewDirty) {
    $("#batch-review-dialog").close();
    if (app.reviewDirty) notify("Save the current invoice before downloading a batch.", "error", 8000);
    return;
  }
  const button = $("#confirm-batch-review");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioBackground("extraction-batch", {
      jobs: jobs.map((job) => ({ id: job.id, revision: job.revision })),
      acknowledge_unvalidated: true,
    });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "Extraction_Batch_REVIEW_ONLY.xlsx"));
    $("#batch-review-dialog").close();
    notify(`${jobs.length} saved invoice${jobs.length === 1 ? "" : "s"} downloaded in one unvalidated review workbook.`);
  } catch (error) {
    notify(error.message, "error", 8000);
  } finally {
    button.disabled = false;
    button.textContent = "Download combined review Excel";
  }
}

// Decision 70: the target workbook for the selected invoices without approving them. The server builds it read-only
// (no export record, no revision bump, nothing learned); a cell the real export would refuse stays empty.
function openBatchDraft() {
  const jobs = selectedBatchReviewJobs();
  if (!jobs.length) return;
  if (app.reviewDirty) {
    notify("Save the current invoice before exporting a draft. Your unsaved edits are still in the form.", "error", 8000);
    $("#save-review").focus();
    return;
  }
  const skipped = app.selectedForBatch.size - jobs.length;
  $("#batch-draft-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#batch-draft-skipped").textContent = skipped ? ` ${skipped} selected without extracted data ${skipped === 1 ? "is" : "are"} left out.` : "";
  $("#batch-draft-skipped").hidden = !skipped;
  $("#batch-draft-dialog").showModal();
}

async function downloadBatchDraft() {
  const jobs = selectedBatchReviewJobs();
  if (!jobs.length || app.reviewDirty) {
    $("#batch-draft-dialog").close();
    if (app.reviewDirty) notify("Save the current invoice before exporting a draft.", "error", 8000);
    return;
  }
  const button = $("#confirm-batch-draft");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioBackground("target-draft", { jobs: jobs.map((job) => ({ id: job.id, revision: job.revision })) });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "DRAFT_Target.xlsx"));
    $("#batch-draft-dialog").close();
    // Decision 72: the server leaves out any invoice without fresh rules and says how many, with the reasons in Checks.
    const included = Number(response.headers.get("x-draft-included") ?? jobs.length);
    const skipped = Number(response.headers.get("x-draft-skipped") ?? 0);
    notify(`${included} invoice${included === 1 ? "" : "s"} in one draft target workbook${skipped ? `; ${skipped} left out, see the Checks sheet` : ""}. Nothing was exported or approved.`, skipped ? "warning" : "success", skipped ? 10000 : undefined);
    // The server's rules refresh can rewrite rules or reset "reviewed" on a stale invoice, so the inbox reloads to show it.
    await loadState().catch(() => {});
  } catch (error) {
    notify(error.message, "error", 8000);
  } finally {
    button.disabled = false;
    button.textContent = "Download draft target workbook";
  }
}

const FINE_RULES_STATUS = { Approved: "success", Review: "warning", Blocked: "error" };

function fineRulesJobIds() {
  return selectedBatchReviewJobs().map((job) => job.id);
}

function fineRulesCell(value) {
  // Blanks stay blank: no placeholder text is invented for a missing value.
  const text = value === null || value === undefined ? "" : String(value);
  return make("td", text ? "" : "fine-rules-blank", text);
}

function fineRulesTable(title, columns, rows, emptyText) {
  const section = make("div", "fine-rules-block");
  section.append(make("h4", "", title));
  if (!rows.length) {
    section.append(make("p", "table-empty", emptyText));
    return section;
  }
  const table = make("table", "line-table fine-rules-table");
  const head = make("tr");
  columns.forEach(([label]) => head.append(make("th", "", label)));
  table.append(make("thead"), make("tbody"));
  table.tHead.append(head);
  rows.forEach((row) => {
    const tr = make("tr");
    columns.forEach(([, key]) => tr.append(fineRulesCell(typeof key === "function" ? key(row) : row[key])));
    table.tBodies[0].append(tr);
  });
  const scroll = make("div", "table-scroll");
  scroll.append(table);
  section.append(scroll);
  return section;
}

function renderFineRulesResult(result) {
  const card = make("details", "fine-rules-invoice");
  card.open = result.status !== "Approved";
  const summary = make("summary");
  const header = result.header || {};
  summary.append(
    make("span", `status-pill ${FINE_RULES_STATUS[result.status] || "neutral"}`, result.status),
    make("strong", "", result.filename || header.Document || ""),
    make("small", "", `${(result.exceptions || []).filter((e) => e.blocking).length} blocking · ${(result.exceptions || []).length} exceptions`),
  );
  card.append(summary);

  const facts = make("dl", "fine-rules-facts");
  const po = result.po || {};
  [["Document", header.Document], ["Supplier Site", header["Supplier Site"]], ["Order No", header["Order No"]],
    ["PO source", po.source], ["Location", header.Location], ["Location Type", header["Location Type"]],
    ["Market", header.Market], ["Currency", header.Currency], ["Document Date", header["Document Date"]],
    ["Net Amount", header["Net Amount"]], ["Tax Amount", header["Tax Amount"]], ["EBS supplier code", result.ebs_supplier_code]]
    .forEach(([label, value]) => {
      const item = make("div");
      const text = value === null || value === undefined ? "" : String(value);
      item.append(make("dt", "", label), make("dd", text ? "" : "fine-rules-blank", text));
      facts.append(item);
    });
  card.append(facts);

  card.append(fineRulesTable("Exceptions", [
    ["Rule ID", "Rule ID"], ["Type", "Exception Type"], ["Line", "Line No."], ["Description", "Description"],
    ["Candidates / evidence", "Candidates / Evidence"], ["Proposed resolution", "Proposed Resolution"],
    ["Blocking", (e) => (e.blocking ? "Yes" : "No")],
  ], result.exceptions || [], "No exceptions."));

  card.append(fineRulesTable("Candidate PO and location", [
    ["PO", "POGRN RMS Order No"], ["Location", "POGRN Location ID"], ["Location type", "Location Type"],
    ["Market", "Market"], ["Qty match", "Qty Match"], ["Pre-tax value match", "Pre-Tax Value Match"],
    ["Checks passed", "Candidate Pass Count"], ["Status", "Validation Status"], ["Reason", "Exception Reason"],
    ["Evidence row", "POGRN Row Reference"],
  ], result.pogrn_validation || [], "No PO/GRN candidate was found for this supplier."));

  const lines = result.lines || [];
  const items = (result.workbench || []).map((row, index) => ({ ...lines[index], ...row }));
  card.append(fineRulesTable("Items", [
    ["Line", "Line No."], ["Barcode", "Barcode"], ["VPN", "VPN"], ["ITEM_PARENT", "ITEM_PARENT"], ["ITEM", "ITEM"],
    ["Match", "Match Method"], ["Rule ID", "Rule ID"], ["Confidence", "Confidence"],
    ["Barcode check", "Barcode Check"], ["VPN check", "VPN Check"], ["Description check", "Description Check"],
    ["Status", "Validation Status"],
  ], items, "No invoice lines."));
  return card;
}

function fineRulesConfigGaps(config) {
  const gaps = [];
  const masterMarkets = Object.values(config?.location_master || {}).some((row) => row?.market);
  if (!masterMarkets && !Object.keys(config?.location_market || {}).length) gaps.push("location→market list (V-007)");
  if (!(config?.supplier_site_currency || []).length && !(config?.supplier_sites || []).length) {
    gaps.push("supplier-site currency list (V-010)");
  }
  return gaps;
}

async function openFineRules() {
  const ids = fineRulesJobIds();
  if (!ids.length) return;
  if (app.reviewDirty) {
    notify("Save the current invoice before running the rules. Your unsaved edits are still in the form.", "error", 8000);
    return;
  }
  app.fineRules = { ids, results: [] };
  $("#fine-rules-count").textContent = `Running on ${ids.length} invoice${ids.length === 1 ? "" : "s"}…`;
  $("#fine-rules-summary").replaceChildren();
  $("#fine-rules-results").replaceChildren();
  $("#fine-rules-target-reason").hidden = true;
  $("#fine-rules-review").disabled = true;
  $("#fine-rules-target").disabled = true;
  $("#fine-rules-dialog").showModal();
  try {
    const [config, run] = await Promise.all([
      api("/api/fine-rules/config"),
      api("/api/fine-rules/run", { method: "POST", body: { job_ids: ids } }),
    ]);
    const gaps = fineRulesConfigGaps(config);
    $("#fine-rules-config-note").hidden = !gaps.length;
    $("#fine-rules-config-text").textContent = gaps.length
      ? `The ${gaps.join(" and ")} ${gaps.length === 1 ? "is" : "are"} missing, so no PO can be confirmed and every invoice stays in Review. The rules never guess these values.`
      : "";
    renderFineRules(run.results || []);
  } catch (error) {
    $("#fine-rules-count").textContent = "The rules could not run.";
    $("#fine-rules-results").replaceChildren(make("p", "validation-summary error", error.message));
  }
}

function renderFineRules(results) {
  app.fineRules.results = results;
  const counts = { Approved: 0, Review: 0, Blocked: 0 };
  results.forEach((result) => { counts[result.status] = (counts[result.status] || 0) + 1; });
  $("#fine-rules-count").textContent = `${results.length} invoice${results.length === 1 ? "" : "s"} checked. Nothing is approved or exported until you download.`;
  $("#fine-rules-summary").replaceChildren(...Object.entries(counts)
    .map(([status, count]) => make("span", `status-pill ${FINE_RULES_STATUS[status]}`, `${count} ${status}`)));
  $("#fine-rules-results").replaceChildren(...results.map(renderFineRulesResult));
  $("#fine-rules-review").disabled = !results.length;
  const notApproved = results.filter((result) => result.status !== "Approved");
  $("#fine-rules-target").disabled = !results.length || notApproved.length > 0;
  $("#fine-rules-target-reason").hidden = !notApproved.length;
  $("#fine-rules-target-reason").textContent = notApproved.length
    ? `Target workbook is available only when every invoice is Approved. Not approved: ${notApproved
      .map((result) => `${result.filename || result.header?.Document || "invoice"} (${result.status})`).join(", ")}.`
    : "";
}

async function downloadFineRules(kind) {
  const ids = app.fineRules?.ids || [];
  if (!ids.length) return;
  const button = $(`#fine-rules-${kind}`);
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioBackground(`fine-rules-${kind}`, { job_ids: ids });
    if (!response.ok) throw await responseError(response);
    const fallback = kind === "target" ? "ULTA_Target.xlsx" : "ULTA_Rules_Review.xlsx";
    saveBlob(await response.blob(), dispositionFilename(response.headers.get("content-disposition") || "", fallback));
  } catch (error) {
    if (kind === "target") {
      $("#fine-rules-target-reason").textContent = error.message;
      $("#fine-rules-target-reason").hidden = false;
    }
    notify(error.message, "error", 8000);
  } finally {
    button.textContent = label;
    button.disabled = kind === "target" ? app.fineRules.results.some((r) => r.status !== "Approved") : false;
  }
}

async function exportBatch() {
  const selected = selectedBatchReviewJobs();
  const jobs = selected.filter(isBatchReady);
  if (!jobs.length || jobs.length !== selected.length) {
    notify("Validated export requires every selected invoice to be approved and ready. Use the review workbook for extracted records.", "error", 8000);
    return;
  }
  if (app.reviewDirty) {
    notify("Save the current invoice before exporting a batch.", "error", 8000);
    return;
  }
  const button = $("#batch-export");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioBackground("export-batch", { jobs: jobs.map((job) => ({ id: job.id, revision: job.revision })) });
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    await downloadApi(result.url, `Merch_Inv_Batch_${result.id.slice(0, 8)}.xlsx`);
    app.selectedForBatch.clear();
    await loadState();
    notify(`${result.count} invoices exported in one workbook.`);
  } catch (error) {
    notify(error.message, "error", 8000);
    await loadState();
  } finally {
    button.textContent = "Export approved";
    updateBatchControls();
  }
}

function activeJobIds() {
  return (app.state?.jobs || [])
    .filter((job) => ["queued", "processing"].includes(job.status))
    .map((job) => job.id);
}

async function pollActiveJobs() {
  if (app.pollBusy) return;
  const active = activeJobIds();
  if (!active.length) {
    clearInterval(app.pollTimer);
    app.pollTimer = null;
    return;
  }

  app.pollBusy = true;
  const selectionToken = app.selectionToken;
  const selectedId = app.selectedJobId;
  const selectedWasProcessing = app.currentJob?.id === selectedId && ["queued", "processing"].includes(app.currentJob.status);
  const ids = [];
  if (selectedWasProcessing && active.includes(selectedId)) ids.push(selectedId);
  const background = active.filter((id) => id !== selectedId);
  for (let count = 0; count < Math.min(2, background.length); count += 1) {
    ids.push(background[(app.pollCursor + count) % background.length]);
  }
  if (background.length) app.pollCursor = (app.pollCursor + Math.min(2, background.length)) % background.length;

  try {
    const responses = await Promise.allSettled(ids.map((id) => api(`/api/jobs/${encodeURIComponent(id)}`)));
    let selectedResult = null;
    let listChanged = false;
    responses.forEach((result) => {
      if (result.status !== "fulfilled") return;
      const job = result.value;
      const stateIndex = app.state.jobs.findIndex((entry) => entry.id === job.id);
      if (stateIndex >= 0) {
        const previous = app.state.jobs[stateIndex];
        listChanged ||= previous.status !== job.status || previous.invoice?.number !== job.invoice?.number ||
          previous.invoice?.supplier_name !== job.invoice?.supplier_name || previous.invoice?.seller !== job.invoice?.seller;
        app.state.jobs[stateIndex] = { ...previous, ...job };
      }
      if (job.id === selectedId) selectedResult = job;
    });
    if (listChanged) renderJobs();

    // A request started for one selection must never replace a document chosen
    // while that request was in flight, or overwrite edits to a completed job.
    if (selectedResult && app.selectionToken === selectionToken && app.selectedJobId === selectedId && selectedWasProcessing) {
      const wasActive = ["queued", "processing"].includes(app.currentJob?.status);
      app.currentJob = selectedResult;
      renderSelectedJob(selectedResult);
      if (wasActive && !["queued", "processing"].includes(selectedResult.status)) {
        notify(selectedResult.status === "error" ? "Invoice processing needs attention." : "Invoice extraction finished.", selectedResult.status === "error" ? "error" : "success");
      }
    }
  } finally {
    app.pollBusy = false;
    if (!activeJobIds().length) {
      clearInterval(app.pollTimer);
      app.pollTimer = null;
    }
  }
}

function startPollingIfNeeded() {
  if (!activeJobIds().length) return;
  if (app.pollTimer) return;
  app.pollTimer = setInterval(pollActiveJobs, 1400);
}

function queueFiles(fileList) {
  const allowed = new Set(["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "docx", "xlsx", "csv", "txt", "json"]);
  const existing = new Set(app.uploadFiles.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
  [...fileList].forEach((file) => {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!allowed.has(ext)) notify(`${file.name}: unsupported file type.`, "error");
    else if (file.size > 12_000_000) notify(`${file.name}: file exceeds 12 MB.`, "error");
    else if (!existing.has(key)) { app.uploadFiles.push(file); existing.add(key); }
  });
  renderUploadQueue();
}

function renderUploadQueue() {
  const queue = $("#upload-queue");
  queue.replaceChildren();
  queue.hidden = !app.uploadFiles.length;
  app.uploadFiles.forEach((file, index) => {
    const row = make("div", "queue-item");
    row.append(make("span", "", `${file.name} · ${formatSize(file.size)}`));
    const remove = make("button", "", "Remove");
    remove.type = "button";
    remove.addEventListener("click", () => { app.uploadFiles.splice(index, 1); renderUploadQueue(); });
    row.append(remove);
    queue.append(row);
  });
  updateUploadReadiness();
}

function providerReady(provider, model) {
  return Boolean(app.state?.connections?.[provider]) && Boolean(String(model || "").trim());
}

function offeredAISettings(settings = {}) {
  const selected = {
    provider: settings.provider || "openai",
    model: String(settings.model || "").trim(),
    ai_fallback: settings.ai_fallback !== false,
  };
  if (providerReady(selected.provider, selected.model)) return selected;
  if (app.state?.connections?.vertex && app.managedVertexModel) {
    return { ...selected, provider: "vertex", model: app.managedVertexModel };
  }
  // The Azure deployment the environment names is the managed default where Vertex is not connected.
  if (app.state?.connections?.azure && app.state?.azure_deployment) {
    return { ...selected, provider: "azure", model: app.state.azure_deployment };
  }
  return selected;
}

async function ensureManagedVertexModel() {
  const settings = app.state?.settings || {};
  if (!app.state?.connections?.vertex || providerReady(settings.provider, settings.model)) return;
  if (app.managedVertexModelsLoaded) return;
  app.managedVertexModelsLoaded = true;
  try {
    const result = await api("/api/connections/vertex/models");
    app.managedVertexModel = String(result.models?.[0]?.id || "").trim();
  } catch (_) {
    app.managedVertexModel = "";
  }
}

function applyManagedVertexModel(prefix) {
  const provider = $(`#${prefix}-provider`);
  if (provider.value !== "vertex" || !app.managedVertexModel) return;
  $(`#${prefix}-model`).value = app.managedVertexModel;
}

function setupModelPicker(prefix) {
  const input = $(`#${prefix}-model`);
  const select = make("select", "model-choices");
  select.id = `${prefix}-model-choices`;
  select.setAttribute("aria-label", `${prefix === "retry" ? "Reprocess" : humanize(prefix)} available models`);
  select.append(new Option("Models load when you choose a connected provider", ""));
  input.before(select);
  input.placeholder = "Model ID · optional manual entry";
  const status = make("small", "model-load-status");
  status.id = `${prefix}-model-status`;
  status.setAttribute("role", "status");
  input.after(status);
  select.addEventListener("change", () => {
    if (select.value === OTHER_MODEL) {
      input.hidden = false;
      input.focus();
      input.select();
    } else {
      if (select.value) input.value = select.value;
      syncModelPicker(prefix);
    }
    if (prefix === "upload") updateUploadReadiness();
  });
}

// A model the provider lists shows once, chosen in the list; the model ID box opens for "Other model ID…", for an
// ID the list does not have, and whenever no list is loaded.
const OTHER_MODEL = "__other_model__";
function syncModelPicker(prefix) {
  const input = $(`#${prefix}-model`);
  const select = $(`#${prefix}-model-choices`);
  if (!input || !select) return;
  const offered = !select.disabled && [...select.options].some((option) => option.value === OTHER_MODEL);
  const listed = offered && Boolean(input.value) && [...select.options].some((option) => option.value === input.value && option.value !== OTHER_MODEL);
  if (offered) select.value = listed ? input.value : input.value ? OTHER_MODEL : "";
  input.hidden = listed;
}

async function loadProviderModels(prefix, { providerChanged = false, force = false } = {}) {
  const provider = $(`#${prefix}-provider`).value;
  const input = $(`#${prefix}-model`);
  const select = $(`#${prefix}-model-choices`);
  const status = $(`#${prefix}-model-status`);
  const token = (app.modelLoadTokens[prefix] || 0) + 1;
  app.modelLoadTokens[prefix] = token;
  if (providerChanged) input.value = "";
  select.replaceChildren(new Option("Loading models…", ""));
  select.disabled = true;
  if (!app.state?.connections?.[provider]) {
    select.replaceChildren(new Option("Connect this provider first", ""));
    status.textContent = `${humanize(provider)} is not connected. Open Engines & AI to connect it.`;
    syncModelPicker(prefix);
    if (prefix === "upload") updateUploadReadiness();
    return;
  }
  syncModelPicker(prefix);
  status.textContent = `Loading models from ${humanize(provider)}…`;
  try {
    let cached = app.providerModels[provider];
    if (force || !cached || Date.now() - cached.at > 300000) {
      cached = { at: Date.now(), promise: api(`/api/connections/${encodeURIComponent(provider)}/models`) };
      app.providerModels[provider] = cached;
    }
    const result = await cached.promise;
    if (app.modelLoadTokens[prefix] !== token || $(`#${prefix}-provider`).value !== provider) return;
    const models = result.models || [];
    app.providerVerified[provider] = true;
    renderConnections();
    select.replaceChildren(new Option(models.length ? "Choose a model" : "No models returned", ""));
    models.forEach((model) => select.add(new Option(model.name && model.name !== model.id ? `${model.name} · ${model.id}` : model.id, model.id)));
    if (models.length) select.add(new Option("Other model ID…", OTHER_MODEL));
    if (!input.value && models.length) {
      const saved = app.state.settings?.provider === provider ? app.state.settings.model : "";
      input.value = models.find((model) => model.id === saved)?.id || models[0].id;
    }
    select.disabled = !models.length;
    status.textContent = models.length ? `${models.length} models available. Check the model before confirming processing.` : "The provider returned no model choices. Check account access or enter a supported model ID.";
  } catch (error) {
    delete app.providerModels[provider];
    app.providerVerified[provider] = false;
    renderConnections();
    if (app.modelLoadTokens[prefix] !== token) return;
    select.replaceChildren(new Option("Models could not be loaded", ""));
    status.textContent = error.message;
  } finally {
    syncModelPicker(prefix);
    if (prefix === "upload") updateUploadReadiness();
  }
}

function updateUploadReadiness() {
  const needsAI = $("#upload-ai-fallback").checked || $("#upload-engine").value === "ai";
  const provider = $("#upload-provider").value;
  const model = $("#upload-model").value.trim();
  $("#upload-ai-options").hidden = !needsAI;
  const prompt = $("#upload-ai-readiness");
  if (needsAI && !providerReady(provider, model)) {
    const connection = app.state?.connections?.[provider];
    prompt.textContent = !connection ? `${humanize(provider)} is not connected. Local extraction can still run; if it cannot finish, the invoice will wait for a connection or manual review.` : "No model is selected. Local extraction can still run; choose a model to make AI fallback available.";
    prompt.hidden = false;
  } else prompt.hidden = true;
  $("#start-upload").disabled = !app.uploadFiles.length || Boolean(app.batch?.uploading) || ($("#upload-engine").value === "ai" && !providerReady(provider, model));
}

function openUpload() {
  const settings = offeredAISettings(app.state?.settings);
  $("#upload-provider").value = settings.provider;
  $("#upload-model").value = settings.model;
  $("#upload-ai-fallback").checked = settings.ai_fallback !== false;
  $("#upload-engine").value = "auto";
  $("#upload-language").value = "en";
  setNativeTextPreference(app.preferNativeText, { remember: false });
  if (app.batch && !batchActive(app.batch)) app.batch = null;  // a finished batch is history; an active one stays in view
  renderBatch();
  updateUploadReadiness();
  $("#upload-dialog").showModal();
  loadProviderModels("upload");
}

function processingOptions(prefix = "upload") {
  const preferNativeText = $(`#${prefix}-prefer-native-text`).checked;
  setNativeTextPreference(preferNativeText);
  return {
    engine: $(`#${prefix}-engine`).value,
    prefer_native_text: preferNativeText,
    ai_fallback: $(`#${prefix}-ai-fallback`).checked,
    provider: $(`#${prefix}-provider`).value,
    model: $(`#${prefix}-model`).value.trim(),
    language: $(`#${prefix}-language`).value,
  };
}

async function beginUploadPreflight(event) {
  event.preventDefault();
  if (!app.uploadFiles.length) return;
  const options = processingOptions();
  try {
    await requestPreflight({
      kind: "upload",
      options,
      files: app.uploadFiles.map((file) => ({ name: file.name, size: file.size })),
    });
  } catch (error) {
    notify(error.message, "error", 7000);
  }
}

// A batch of a hundred-plus files: three uploads at a time, each retried with back-off when the server is busy,
// restarting or unreachable. Files that still fail stay listed with a one-click retry; nothing has to be re-picked.
const UPLOAD_PARALLEL = 3;
const UPLOAD_ATTEMPTS = 6;
const UPLOAD_BACKOFF_MS = 1000;
const UPLOAD_BACKOFF_MAX_MS = 30000;
const PLAN_RENEW_MS = 240000;  // a confirmed plan lasts ten minutes; renew it every four while uploads are in flight
const BATCH_POLL_MS = 1500;
const BATCH_SETTLED_PER_POLL = 6;

function uploadRetryWait(response, delay) {
  const retryAfter = Number(response?.headers?.get?.("retry-after")) * 1000;
  return retryAfter > 0 ? Math.min(retryAfter, UPLOAD_BACKOFF_MAX_MS) : delay;
}

async function uploadInvoice(file, options, planToken, { attempts = UPLOAD_ATTEMPTS, onWait = () => {} } = {}) {
  let delay = UPLOAD_BACKOFF_MS;
  for (let attempt = 1; ; attempt += 1) {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("options", JSON.stringify(options));
    form.append("preflight_token", planToken);
    let response = null;
    try {
      response = await studioFetch("/api/invoices", { method: "POST", body: form });
    } catch (_) { /* unreachable: retried below */ }
    if (response?.ok) return await response.json();
    // 429 (queue full, server starting), 5xx and a dropped connection pass; a refused file (4xx) is final.
    const transient = !response || response.status === 429 || response.status >= 500;
    if (!transient || attempt >= attempts) throw response ? await responseError(response) : new Error("Invoice Studio could not reach the server.");
    const wait = uploadRetryWait(response, delay);
    onWait(`${response ? "server busy" : "connection lost"}, retrying in ${Math.max(1, Math.round(wait / 1000))} s`);
    await new Promise((resolve) => setTimeout(resolve, wait));
    delay = Math.min(delay * 2, UPLOAD_BACKOFF_MAX_MS);
  }
}

function planExpiredError(message) {
  return /processing plan expired|review and confirm the processing rules/i.test(message || "");
}

function batchActive(batch) {
  return Boolean(batch) && (batch.uploading || batch.entries.some((entry) => entry.jobId && ["queued", "processing"].includes(entry.status)));
}

function batchCounts(entries) {
  const counts = { pending: 0, uploading: 0, queued: 0, processing: 0, done: 0, failed: 0 };
  entries.forEach((entry) => { counts[entry.status] += 1; });
  return counts;
}

function renderBatch() {
  const panel = $("#upload-progress");
  const batch = app.batch;
  if (!batch) { panel.hidden = true; panel.replaceChildren(); return; }
  const counts = batchCounts(batch.entries);
  const total = batch.entries.length;
  const uploaded = batch.entries.filter((entry) => entry.jobId).length;
  const waiting = batch.entries.filter((entry) => entry.note).length;
  let headline = batch.uploading ? `Uploading ${Math.min(total, uploaded + counts.uploading)} of ${total}, ${UPLOAD_PARALLEL} at a time` : `${uploaded} of ${total} uploaded`;
  if (waiting) headline += ` · ${waiting} waiting to retry`;
  if (!batch.uploading && counts.failed === 0 && counts.queued + counts.processing === 0) headline = `${total} of ${total} processed. Review them in the inbox.`;
  const head = make("p", "batch-progress-line", headline);
  const meters = make("div", "batch-progress-counts");
  [["Queued", counts.queued], ["Processing", counts.processing], ["Done", counts.done], ["Failed", counts.failed]].forEach(([label, count]) => {
    meters.append(make("span", `batch-count batch-count-${label.toLowerCase()}`, `${label} ${count}`));
  });
  panel.replaceChildren(head, meters);
  const failed = batch.entries.filter((entry) => entry.status === "failed");
  if (failed.length) {
    const list = make("div", "batch-failed");
    const retryable = failed.filter((entry) => !entry.jobId);
    const expired = retryable.filter((entry) => planExpiredError(entry.error));
    if (expired.length) {
      const again = make("button", "button button-secondary batch-retry-all", `Review rules again for ${expired.length} file${expired.length === 1 ? "" : "s"}`);
      again.type = "button";
      again.addEventListener("click", () => replanUploads(expired));
      list.append(again);
    } else if (retryable.length > 1) {
      const all = make("button", "button button-secondary batch-retry-all", `Retry all ${retryable.length} failed uploads`);
      all.type = "button";
      all.addEventListener("click", () => retryUploads(retryable));
      list.append(all);
    }
    failed.forEach((entry) => {
      const row = make("div", "queue-item batch-failed-item");
      row.append(make("span", "", `${entry.file.name}: ${entry.error || "processing failed"}`));
      if (!entry.jobId) {
        const retry = make("button", "", planExpiredError(entry.error) ? "Review rules" : "Retry");
        retry.type = "button";
        retry.disabled = Boolean(batch.uploading);
        retry.addEventListener("click", () => (planExpiredError(entry.error) ? replanUploads([entry]) : retryUploads([entry])));
        row.append(retry);
      }
      list.append(row);
    });
    panel.append(list);
  }
  panel.hidden = false;
}

function startPlanRenewal(batch) {
  if (batch.renewTimer) return;
  batch.renewTimer = setInterval(async () => {
    const tokens = new Set(batch.entries.filter((entry) => ["pending", "uploading"].includes(entry.status)).map((entry) => entry.token));
    for (const token of tokens) {
      try { await api("/api/preflight/renew", { method: "POST", body: { token } }); }
      catch (_) { /* an expired plan is reported by the upload it refuses */ }
    }
  }, PLAN_RENEW_MS);
}

function stopPlanRenewal(batch) {
  clearInterval(batch.renewTimer);
  batch.renewTimer = null;
}

async function uploadEntry(batch, entry) {
  entry.status = "uploading";
  entry.error = "";
  renderBatch();
  try {
    const job = await uploadInvoice(entry.file, entry.options, entry.token, { onWait: (note) => { entry.note = note; renderBatch(); } });
    entry.jobId = job.id;
    // The server answers "queued", or "error" when it refused the document itself (a combined multi-invoice PDF).
    entry.status = job.status === "error" ? "failed" : job.status;
    entry.error = job.error || "";
    replaceJobSummary(job);
  } catch (error) {
    entry.status = "failed";
    entry.error = error.message;
  }
  entry.note = "";
  renderBatch();
}

async function uploadBatchEntries(batch) {
  const queue = batch.entries.filter((entry) => entry.status === "pending");
  if (!queue.length) return;
  batch.uploading = true;
  updateUploadReadiness();
  startPlanRenewal(batch);
  try {
    const worker = async () => { while (queue.length) await uploadEntry(batch, queue.shift()); };
    await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, queue.length) }, worker));
  } finally {
    stopPlanRenewal(batch);
    batch.uploading = false;
    updateUploadReadiness();
    renderBatch();
  }
}

async function pollBatch(batch) {
  if (batch.pollBusy) return;
  batch.pollBusy = true;
  try {
    const queue = await api("/api/queue");
    const queued = new Set(queue.queued);
    const processing = new Set(queue.processing);
    const settled = [];
    batch.entries.forEach((entry) => {
      if (!entry.jobId || !["queued", "processing"].includes(entry.status)) return;
      if (processing.has(entry.jobId)) entry.status = "processing";
      else if (queued.has(entry.jobId)) entry.status = "queued";
      else settled.push(entry);
    });
    // Jobs leave the queue a few at a time: their final state is fetched once each, never the whole batch.
    for (const entry of settled.slice(0, BATCH_SETTLED_PER_POLL)) {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(entry.jobId)}`);
        entry.status = job.status === "error" ? "failed" : ["queued", "processing"].includes(job.status) ? job.status : "done";
        entry.error = job.status === "error" ? job.error || "processing failed" : "";
        replaceJobSummary(job);
      } catch (error) {
        entry.status = "failed";
        entry.error = error.message;
      }
    }
    renderBatch();
  } catch (_) {
    /* a missed poll is retried on the next tick */
  } finally {
    batch.pollBusy = false;
    if (!batchActive(batch)) stopBatchPolling(batch);
  }
}

function startBatchPolling(batch) {
  if (batch.pollTimer || !batchActive(batch)) return;
  batch.pollTimer = setInterval(() => pollBatch(batch), BATCH_POLL_MS);
}

function stopBatchPolling(batch) {
  clearInterval(batch.pollTimer);
  batch.pollTimer = null;
}

async function finishUploads(batch) {
  await loadState({ preserveSelection: false });
  revealNewestJobs();
  startBatchPolling(batch);
  const queued = batch.entries.filter((entry) => entry.jobId).length;
  const failed = batch.entries.filter((entry) => entry.status === "failed" && !entry.jobId).length;
  if (failed) notify(`${failed} file${failed === 1 ? "" : "s"} could not be added. Retry ${failed === 1 ? "it" : "them"} from the list.`, "error", 8000);
  else notify(`${queued} invoice${queued === 1 ? "" : "s"} queued. Several process at once; the rest wait their turn.`);
}

async function runUpload(preflightToken, options) {
  const previous = app.batch && batchActive(app.batch) ? app.batch : null;
  if (previous) stopBatchPolling(previous);
  const entries = app.uploadFiles.map((file) => ({ file, token: preflightToken, options, status: "pending", jobId: null, error: "", note: "" }));
  const batch = { entries: [...(previous?.entries || []), ...entries], uploading: false, renewTimer: null, pollTimer: null, pollBusy: false };
  app.batch = batch;
  app.uploadFiles = [];
  renderUploadQueue();
  $("#upload-dialog").showModal();
  renderBatch();
  const first = entries;
  await uploadBatchEntries(batch);
  await finishUploads(batch);
  const selected = first.find((entry) => entry.jobId);
  if (selected) await selectJob(selected.jobId);
}

async function retryUploads(entries) {
  const batch = app.batch;
  if (!batch || batch.uploading) return;
  entries.forEach((entry) => { entry.status = "pending"; entry.error = ""; });
  renderBatch();
  await uploadBatchEntries(batch);
  await finishUploads(batch);
}

function replanUploads(entries) {
  // The plan these files were confirmed under has expired: they go back to the queue for a fresh plan.
  const batch = app.batch;
  if (!batch || batch.uploading) return;
  batch.entries = batch.entries.filter((entry) => !entries.includes(entry));
  if (!batch.entries.length) app.batch = null;
  queueFiles(entries.map((entry) => entry.file));
  renderBatch();
  $("#start-upload").focus();
}

async function prepareDemo() {
  const buttons = [$("#load-demo")];
  buttons.forEach((button) => { button.disabled = true; button.textContent = "Preparing demo…"; });
  try {
    await api("/api/references/demo", { method: "POST", body: {} });
    await loadState();
    const sample = app.state.samples?.demo;
    if (!sample) throw new Error("Demo invoice metadata is unavailable.");
    await requestPreflight({
      kind: "demo",
      options: { engine: "auto", prefer_native_text: true, ai_fallback: true, provider: "openai", model: "", language: "en" },
      files: [{ name: sample.name, size: sample.size }],
    });
  } catch (error) {
    notify(error.message, "error", 7000);
  } finally {
    buttons.forEach((button) => { button.disabled = false; button.textContent = "Try synthetic demo"; });
  }
}

async function runDemo(preflightToken) {
  const job = await api("/api/demo", { method: "POST", body: { preflight_token: preflightToken } });
  await loadState({ preserveSelection: false });
  revealNewestJobs();
  await selectJob(job.id);
  notify("Synthetic reference data and invoice loaded. No real business data was used.");
}

function renderPreflight(plan, request) {
  const files = $("#preflight-files");
  files.replaceChildren();
  request.files.forEach((file) => files.append(make("span", "preflight-file", `${file.name} · ${formatSize(file.size)}`)));

  const summary = $("#preflight-summary");
  summary.replaceChildren();
  const details = [
    ["Files", String(plan.summary.file_count)],
    ["Engine", humanize(plan.summary.engine)],
    ["PDF text", plan.summary.prefer_native_text ? "Use when available (faster)" : "Disabled — force the selected OCR reader"],
    ["AI fallback", plan.summary.ai_fallback ? "Allowed" : "Disabled (local only)"],
    ["Provider", plan.summary.ai_fallback || plan.summary.engine === "ai" ? humanize(plan.summary.provider) : "Not used"],
    ["Model", plan.summary.ai_fallback || plan.summary.engine === "ai" ? (plan.summary.model || "Not selected") : "Not used"],
    ["References", !app.state?.legacy_references ? "Owner catalog and mapping tables"
      : plan.summary.reference_version ? `Version ${String(plan.summary.reference_version).slice(0, 12)}` : "Not loaded — export will be held"],
    ["Price tolerance", String(plan.summary.policy?.price_tolerance ?? "—")],
    ["Total tolerance", String(plan.summary.policy?.total_tolerance ?? "—")],
  ];
  details.forEach(([term, description]) => {
    const row = make("div");
    row.append(make("dt", "", term), make("dd", "", description));
    summary.append(row);
  });

  const populateNotes = (selector, notes) => {
    const block = $(selector);
    block.hidden = !notes?.length;
    const list = $("ul", block);
    list.replaceChildren();
    (notes || []).forEach((note) => list.append(make("li", "", note)));
  };
  populateNotes("#preflight-warnings", plan.warnings);
  populateNotes("#preflight-blocking", plan.blocking);
  $("#confirm-preflight").disabled = Boolean(plan.blocking?.length);
}

async function requestPreflight(request) {
  const plan = await api("/api/preflight", { method: "POST", body: { options: request.options, files: request.files } });
  app.preflight = { ...request, token: plan.token };
  renderPreflight(plan, request);
  if ($("#upload-dialog").open) $("#upload-dialog").close();
  if ($("#retry-dialog").open) $("#retry-dialog").close();
  $("#preflight-dialog").showModal();
}

async function confirmPreflight(event) {
  event.preventDefault();
  const pending = app.preflight;
  if (!pending) return;
  const button = $("#confirm-preflight");
  button.disabled = true;
  button.textContent = "Confirming…";
  try {
    await api("/api/preflight/confirm", { method: "POST", body: { token: pending.token } });
    $("#preflight-dialog").close();
    app.preflight = null;
    if (pending.kind === "upload") await runUpload(pending.token, pending.options);
    else if (pending.kind === "demo") await runDemo(pending.token);
    else if (pending.kind === "retry") await runRetry(pending.token, pending.options);
  } catch (error) {
    notify(error.message, "error", 8000);
  } finally {
    button.textContent = "Confirm rules and process";
    button.disabled = false;
  }
}

function cancelPreflight() {
  const pending = app.preflight;
  app.preflight = null;
  if (pending?.kind === "upload" && !$("#upload-dialog").open) $("#upload-dialog").showModal();
  if (pending?.kind === "retry" && !$("#retry-dialog").open) $("#retry-dialog").showModal();
}

function referenceValue(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function referenceRecordTitle(record) {
  const candidateDescription = candidateValues(record, "description")[0];
  if (candidateDescription) return candidateDescription;
  const entries = Object.entries(record.data || {}).filter(([, value]) => referenceValue(value).trim());
  const preferred = [
    /product.*name|item.*name|description/i,
    /sku|item.*code|product.*code/i,
    /barcode|gtin|upc/i,
    /po.*number|purchase.*order|\bpo\b/i,
    /supplier|vendor/i,
  ];
  for (const pattern of preferred) {
    const match = entries.find(([name]) => pattern.test(name));
    if (match) return referenceValue(match[1]);
  }
  return entries.length ? referenceValue(entries[0][1]) : `${humanize(record.kind || "source")} record`;
}

function candidateValues(record, field, sourceColumn = "") {
  if (!sourceColumn) {
    const values = record.candidate_fields?.[field];
    if (!Array.isArray(values)) return [];
    return [...new Set(values.map((value) => referenceValue(value).trim()).filter(Boolean))];
  }
  const values = (record.candidate_field_sources?.[field] || [])
    .filter((source) => referenceValue(source?.column).trim() === sourceColumn)
    .map((source) => referenceValue(source?.value).trim());
  const direct = record.data?.[sourceColumn];
  const directValues = Array.isArray(direct) ? direct : [direct];
  directValues.forEach((value) => values.push(referenceValue(value).trim()));
  return [...new Set(values.filter(Boolean))];
}

function beginLineReferenceLookup(mode, row) {
  const isDraft = mode === "draft";
  const value = (name) => $(`[name="${name}"]`, row)?.value.trim() || "";
  const query = isDraft
    ? row.dataset.lookupDescription || value("Item") || value("UPC")
    : value("description") || value("sku") || value("gtin");
  if (query.length < 2) {
    notify("Enter a product description, item code or barcode before searching.", "error");
    return;
  }
  app.referenceLookupTarget = { mode, row, context: {
    price: value(isDraft ? "Unit Cost" : "price"), qty: value(isDraft ? "Quantity" : "qty"),
    uom: isDraft ? "" : value("uom"),
    currency: $('#header-fields [name="currency"]')?.value.trim() || app.currentJob?.invoice?.currency || "",
    invoice_po: $('#header-fields [name="po"]')?.value.trim() || app.currentJob?.invoice?.po || "",
  } };
  if (isDraft) $("#manual-draft-dialog").close();
  navigate("references");
  $("#reference-lookup-kind").value = "item";
  $("#reference-lookup-query").value = query.slice(0, 100);
  $("#reference-lookup-site").value = "";
  $("#reference-lookup-supplier").value = "";
  $("#reference-lookup-po").value = "";
  searchReferenceLookup();
}

function candidateSelect(record, field, labelText, sourceColumn = "") {
  const label = make("label");
  label.append(make("span", "", labelText));
  const select = make("select");
  select.dataset.candidateField = field;
  const values = candidateValues(record, field, sourceColumn);
  if (values.length !== 1) {
    const placeholder = make("option", "", values.length ? "Choose a value" : "Not available");
    placeholder.value = "";
    select.append(placeholder);
  }
  values.forEach((value) => {
    const columns = [...new Set((record.candidate_field_sources?.[field] || [])
      .filter((source) => referenceValue(source?.value).trim() === value
        && (!sourceColumn || referenceValue(source?.column).trim() === sourceColumn))
      .map((source) => referenceValue(source?.column).trim())
      .filter(Boolean))];
    if (sourceColumn && !columns.length) columns.push(sourceColumn);
    const option = make("option", "", columns.length ? `${columns.join(" / ")}: ${value}` : value);
    option.value = value;
    select.append(option);
  });
  if (values.length === 1) select.value = values[0];
  select.disabled = !values.length;
  label.append(select);
  return label;
}

function applyReferenceCandidate(action) {
  if (!$(".candidate-confirm", action)?.checked) return;
  const target = app.referenceLookupTarget;
  if (!target?.row?.isConnected) {
    notify("That invoice line is no longer available. Open it and search again.", "error");
    app.referenceLookupTarget = null;
    return;
  }
  const selected = Object.fromEntries($$("select[data-candidate-field]", action).map((select) => [select.dataset.candidateField, select.value]));
  if (!selected.sku && !selected.internal_item && !selected.gtin && !selected.uom) return;
  const names = target.mode === "draft" ? { internal_item: "Item", gtin: "UPC" } : { internal_item: "item_id", sku: "sku", gtin: "gtin", uom: "uom", description: "description" };
  Object.entries(names).forEach(([field, inputName]) => {
    if (!selected[field]) return;
    const input = $(`[name="${inputName}"]`, target.row);
    input.value = selected[field];
    // As if typed: a filled field hides its "Not read" note (decision 160) and draft totals update.
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const mode = target.mode;
  const evidence = $('[name="evidence"]', target.row);
  if (mode === "review" && evidence && action.dataset.sourceReference) {
    evidence.value = `${evidence.value}\nHuman-confirmed reference: ${action.dataset.sourceReference}`.trim();
  }
  if (mode === "review") markReviewDirty();
  app.referenceLookupTarget = null;
  navigate("workspace");
  if (mode === "draft") $("#manual-draft-dialog").showModal();
  notify("Candidate values copied for review. They are not approved until you verify and save them.");
}

function referenceCandidateAction(record) {
  if (!app.referenceLookupTarget || record.kind !== "item" || !record.candidate_fields) return null;
  const action = make("div", "reference-candidate-action");
  action.dataset.sourceReference = `${record.source_hash} / ${record.source_sheet} / row ${record.source_row}`;
  const contextValues = [
    ["description", "Description"], ["pack", "Pack"], ["supplier", "Supplier"], ["site", "Site"], ["po", "PO"],
  ].flatMap(([field, label]) => {
    const values = candidateValues(record, field);
    return values.length ? [`${label}: ${values.join(" / ")}`] : [];
  });
  if (contextValues.length) action.append(make("p", "reference-candidate-context", contextValues.join(" · ")));
  action.append(candidateSelect(record, "internal_item", "Internal item", "ITEM_PARENT"));
  if (app.referenceLookupTarget.mode !== "draft") action.append(candidateSelect(record, "sku", "Supplier SKU"));
  action.append(candidateSelect(record, "gtin", "Barcode / GTIN"));
  if (app.referenceLookupTarget.mode === "review") {
    action.append(candidateSelect(record, "uom", "Unit of measure"));
    action.append(candidateSelect(record, "description", "Master product name", record.data?.ITEM_DESC ? "ITEM_DESC" : ""));
  }
  const confirmation = make("label", "candidate-confirm-label");
  const confirmed = make("input", "candidate-confirm"); confirmed.type = "checkbox";
  confirmation.append(confirmed, make("span", "", "I confirm this is the correct product and pack. Keep the invoice price and quantity unchanged."));
  action.append(confirmation);
  const buttons = make("div", "reference-candidate-buttons");
  const use = make("button", "button button-secondary", "Use selected item");
  use.type = "button";
  const findPO = make("button", "button button-quiet", "Find PO/GRN rows");
  findPO.type = "button";
  const update = () => {
    use.disabled = !confirmed.checked || !$("select[data-candidate-field]:not(:disabled)", action) || !$$('select[data-candidate-field]', action).some((select) => select.value);
    findPO.disabled = !$('[data-candidate-field="gtin"]', action)?.value;
  };
  action.addEventListener("change", update);
  use.addEventListener("click", () => applyReferenceCandidate(action));
  findPO.addEventListener("click", () => {
    const gtin = $('[data-candidate-field="gtin"]', action)?.value;
    if (!gtin) return;
    $("#reference-lookup-kind").value = "po";
    $("#reference-lookup-query").value = gtin;
    $("#reference-lookup-site").value = "";
    $("#reference-lookup-supplier").value = "";
    $("#reference-lookup-po").value = String(app.currentJob?.invoice?.po || "").trim();
    searchReferenceLookup();
  });
  buttons.append(use, findPO);
  action.append(buttons);
  update();
  return action;
}

function renderReferenceRecord(record) {
  const details = make("details", "reference-result");
  const summary = make("summary");
  const title = make("span", "reference-result-title");
  title.append(make("strong", "", referenceRecordTitle(record)));
  const match = record.match || {};
  const matchBasis = match.basis ?? record.match_basis;
  const basis = matchBasis ? `Matched by ${Array.isArray(matchBasis) ? matchBasis.map(referenceValue).join(", ") : referenceValue(matchBasis)}` : "Candidate source row";
  title.append(make("small", "", basis));
  const provenance = [record.source_sheet, record.source_row ? `row ${record.source_row}` : "", record.source_hash ? `source ${String(record.source_hash).slice(0, 10)}` : ""].filter(Boolean).join(" · ");
  const meta = make("span", "reference-result-meta", provenance);
  const score = match.score ?? record.score;
  if (score !== undefined && score !== null) meta.append(make("small", "", ` · score ${score}`));
  summary.append(title, meta);
  details.append(summary);

  const body = make("div", "reference-result-body");
  if (match.name_similarity !== undefined) body.append(make("p", "reference-candidate-context", `Product-name similarity: ${Math.round(match.name_similarity * 100)}%. This is a search clue, not an approval or accuracy measurement.`));
  for (const clue of match.clues || []) {
    const facts = [`PO ${clue.po || "—"}, source row ${clue.source_row}`, `Price ${clue.unit_price ?? "—"} ${clue.currency || ""}`, `Ordered ${clue.ordered_qty ?? "—"}; received ${clue.received_qty ?? "—"}`];
    if (clue.price_similarity !== undefined) facts.push(`Price similarity ${Math.round(clue.price_similarity * 100)}%`);
    if (clue.quantity_similarity !== undefined) facts.push(`Raw quantity similarity ${Math.round(clue.quantity_similarity * 100)}%`);
    body.append(make("p", "reference-candidate-context", facts.join(" · ")));
    if (clue.price_note || clue.quantity_note) body.append(make("p", "reference-candidate-context", [clue.price_note, clue.quantity_note].filter(Boolean).join(". ")));
  }
  const candidateAction = referenceCandidateAction(record);
  if (candidateAction) body.append(candidateAction);
  const flags = Array.isArray(record.flags) ? record.flags : [];
  if (flags.length) {
    const flagList = make("div", "reference-result-flags");
    flags.forEach((flag) => flagList.append(make("span", "", referenceValue(flag))));
    body.append(flagList);
  }
  const data = make("dl", "reference-result-data");
  Object.entries(record.data || {}).forEach(([name, value]) => {
    data.append(make("dt", "", name), make("dd", "", referenceValue(value) || "—"));
  });
  body.append(data);
  details.append(body);
  return details;
}

async function ensureReferenceLookup() {
  if (app.referenceLookupLoaded) return;
  app.referenceLookupLoaded = true;
  try {
    const summary = await api("/api/reference-lookup/summary");
    const container = $("#reference-source-summary");
    container.replaceChildren();
    const sources = Array.isArray(summary.sources) ? summary.sources : [];
    if (!sources.length) container.append(make("span", "", "No staged source evidence is available yet."));
    sources.forEach((source) => {
      if (source.counts && typeof source.counts === "object") {
        const counts = Object.entries(source.counts)
          .filter(([kind, count]) => kind !== "total" && Number.isFinite(Number(count)))
          .map(([kind, count]) => `${Number(count).toLocaleString()} ${humanize(kind).toLowerCase()} rows`)
          .join(" · ");
        container.append(make("span", "", `${source.id || "Staged source"}${counts ? ` · ${counts}` : ""}`));
      } else {
        const count = Number.isFinite(Number(source.count)) ? `${Number(source.count).toLocaleString()} records` : "record count unavailable";
        const hash = source.hash ? ` · ${String(source.hash).slice(0, 10)}` : "";
        container.append(make("span", "", `${humanize(source.kind || "source")} · ${count}${hash}`));
      }
    });
    if (summary.notice) {
      $("#reference-lookup-notice").textContent = summary.notice;
      $("#reference-lookup-notice").hidden = false;
    }
  } catch (error) {
    app.referenceLookupLoaded = false;
    $("#reference-source-summary").replaceChildren(make("span", "", "Staged source evidence could not be loaded."));
    throw error;
  }
}

async function searchReferenceLookup({ append = false } = {}) {
  const query = (append ? app.referenceLookupQuery : $("#reference-lookup-query").value.trim());
  const kind = append ? app.referenceLookupKind : $("#reference-lookup-kind").value;
  const filters = append ? app.referenceLookupFilters : {
    site: $("#reference-lookup-site").value.trim(),
    supplier: $("#reference-lookup-supplier").value.trim(),
    po: $("#reference-lookup-po").value.trim(),
  };
  if (query.length < 2) {
    $("#reference-lookup-query").focus();
    return;
  }
  const button = append ? $("#reference-lookup-more") : $("#reference-lookup-submit");
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = append ? "Loading…" : "Searching…";
  try {
    const params = new URLSearchParams({ kind, q: query, limit: "25" });
    Object.entries(filters).forEach(([name, value]) => { if (value) params.set(name, value); });
    if (append && app.referenceLookupCursor) params.set("cursor", app.referenceLookupCursor);
    if (kind === "item") Object.entries(app.referenceLookupTarget?.context || {}).forEach(([name, value]) => { if (value) params.set(name, value); });
    const response = await api(`/api/reference-lookup/${kind === "item" ? "products" : "search"}?${params}`);
    const results = $("#reference-lookup-results");
    if (!append) results.replaceChildren();
    const records = Array.isArray(response.records) ? response.records : [];
    records.forEach((record) => results.append(renderReferenceRecord(record)));
    if (!append && !records.length) results.append(make("p", "table-empty", "No staged source rows matched this search."));
    app.referenceLookupQuery = query;
    app.referenceLookupKind = kind;
    app.referenceLookupFilters = filters;
    app.referenceLookupCursor = response.next_cursor || null;
    $("#reference-lookup-more").hidden = !app.referenceLookupCursor;
    $("#reference-lookup-notice").textContent = response.notice || "Candidate source rows are evidence only and require confirmation.";
    $("#reference-lookup-notice").hidden = false;
  } catch (error) {
    $("#reference-lookup-notice").textContent = error.message;
    $("#reference-lookup-notice").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

// Mapping tables: report cells and filenames are always inserted as text.
let mappingUploadId = null;
let mappingBusy = false;

function mappingReport(report) {
  const block = make("div", "");
  if (!report) {
    block.append(make("p", "", "Configuration snapshot saved before replacement; no source workbook."));
    return block;
  }
  block.append(make("p", "", report.valid ? "Validation passed. Review counts before activating." : "Validation failed. Correct the flagged rows and upload again."));
  const table = make("table", "");
  const heading = make("tr", "");
  ["Sheet", "Rows", "Accepted", "Excluded", "Flagged rows"].forEach((label) => heading.append(make("th", "", label)));
  const head = make("thead", ""); head.append(heading); table.append(head);
  const body = make("tbody", "");
  Object.entries(report.sheets).forEach(([name, counts]) => {
    const row = make("tr", "");
    [name, counts.rows, counts.accepted, counts.excluded, counts.flagged].forEach((value) => row.append(make("td", "", String(value))));
    body.append(row);
  });
  table.append(body); block.append(table);
  if (report.flagged.length) {
    const details = make("details", ""); details.open = true;
    details.append(make("summary", "", `${report.flagged.length} validation issue(s)`));
    const list = make("ul", "");
    report.flagged.forEach((flag) => list.append(make("li", "", `${flag.sheet} · row ${flag.row}: ${flag.reason}`)));
    details.append(list); block.append(details);
  }
  block.append(make("p", "", "Excluded VAT rows are outside type C / code containing PV. Row 1 issues refer to sheet or header checks."));
  return block;
}

async function loadMappingTables() {
  if (app.authMode !== "local" && app.sessionUser?.role !== "admin") return;
  const data = await api("/api/mapping-tables");
  const current = data.versions.find((version) => version.id === data.active?.id);
  $("#mapping-active").textContent = current
    ? `Active: ${current.filename} · ${current.version}. Activated ${data.active.activated_at} by ${data.active.activated_by}.`
    : "The current rules configuration has no active uploaded version. Upload a workbook to create one.";
  $("#mapping-download").hidden = !current?.has_workbook;
  const history = $("#mapping-history"); history.replaceChildren();
  if (!data.versions.length) history.append(make("p", "", "No saved versions yet."));
  data.versions.forEach((version) => {
    const entry = make("details", "");
    entry.append(make("summary", "", `${version.filename} · ${version.created_at} · ${version.created_by}${version.id === current?.id ? " · Active" : ""}`));
    entry.append(make("p", "", `Version: ${version.version}`), mappingReport(version.report));
    if (version.id !== current?.id) {
      const rollback = make("button", "button button-secondary", "Roll back to this version");
      rollback.type = "button";
      rollback.addEventListener("click", () => changeMappingVersion(`/api/mapping-tables/versions/${version.id}/rollback`, rollback));
      entry.append(rollback);
    }
    history.append(entry);
  });
}

async function validateMappingUpload(event) {
  event.preventDefault();
  if (mappingBusy) return;
  mappingUploadId = null;
  $("#mapping-activate").hidden = true;
  $("#mapping-report").replaceChildren();
  const file = $("#mapping-file").files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".xlsx") || file.size > 8000000) {
    notify("Choose an .xlsx workbook up to 8 MB.", "error"); return;
  }
  mappingBusy = true; $("#mapping-validate").disabled = true; $("#mapping-file").disabled = true;
  try {
    const form = new FormData(); form.append("file", file);
    const result = await api("/api/mapping-tables/validate", { method: "POST", body: form });
    $("#mapping-report").append(make("p", "", result.filename), mappingReport(result.report));
    mappingUploadId = result.upload_id;
    $("#mapping-activate").hidden = !mappingUploadId;
  } catch (error) { notify(error.message, "error"); }
  finally { mappingBusy = false; $("#mapping-validate").disabled = false; $("#mapping-file").disabled = false; }
}

async function changeMappingVersion(path, button) {
  if (mappingBusy) return;
  mappingBusy = true; button.disabled = true;
  try {
    await api(path, { method: "POST" });
    mappingUploadId = null; $("#mapping-activate").hidden = true;
    $("#mapping-report").replaceChildren();
    await loadMappingTables();
    await loadState();
    if (app.selectedJobId && !app.reviewDirty) await selectJob(app.selectedJobId);
    notify("Mapping version activated. Invoice results refresh when opened; exported invoices keep their saved results.");
  } catch (error) { notify(error.message, "error"); }
  finally { mappingBusy = false; button.disabled = false; }
}

function renderReferences() {
  $("#mapping-tables-card").hidden = app.authMode !== "local" && app.sessionUser?.role !== "admin";
  // Production validates against the owner catalog and mapping tables; the legacy snapshot and the synthetic demo
  // that loads it exist only in test builds, where the server answers their endpoints.
  const legacy = Boolean(app.state?.legacy_references);
  $("#load-demo").hidden = !legacy;
  $("#legacy-reference-card").hidden = !legacy;
  const refs = app.state?.references;
  $("#reference-status").textContent = refs ? `Version ${String(refs.version || "loaded").slice(0, 10)}` : "Not loaded";
  $("#reference-status").className = `status-pill ${refs ? "success" : "neutral"}`;
  const summary = $("#reference-summary");
  summary.replaceChildren();
  summary.hidden = !refs;
  if (refs) Object.entries(refs.counts || {}).forEach(([name, count]) => {
    const stat = make("div", "reference-stat");
    stat.append(make("strong", "", String(count)), make("span", "", humanize(name)));
    summary.append(stat);
  });
}

function renderPolicy() {
  if (!app.state?.policy) return;
  const form = $("#policy-form");
  form.elements.price_tolerance.value = app.state.policy.price_tolerance ?? "0";
  form.elements.total_tolerance.value = app.state.policy.total_tolerance ?? "0.01";
  form.elements.currency_decimals.value = JSON.stringify(app.state.policy.currency_decimals || {}, null, 2);
}

function renderEngines() {
  const list = $("#engine-list");
  list.replaceChildren();
  const engineHelp = {
    invoice2data: "Reads native PDF/text and supplier templates. Scanned PDFs and images use PaddleOCR locally as its input reader; the trace shows both components.",
    paddleocr: "Uses PaddleOCR for scanned text, then layout rules convert recognized labels and rows into invoice fields.",
    docling: "Converts Docling layout and table cells into invoice fields, with RapidOCR / Paddle for scanned pages. Difficult scans may still need the direct PaddleOCR reader or permitted AI fallback.",
  };
  const localQualityTarget = "Quality target: at least 90% source-correct available fields, 95% expected and 100% ideal. This target is not a measured result; test each reader separately, including line recall.";
  (app.state?.engines || []).forEach((engine) => {
    const item = make("div", "engine-item");
    const head = make("div", "engine-item-head");
    head.append(make("strong", "", humanize(engine.id)));
    const dot = make("span", `availability${engine.installed ? " yes" : ""}`);
    dot.title = engine.installed ? "Installed" : "Not installed";
    head.append(dot);
    item.append(head, make("p", "", engine.note || (engine.installed ? "Available" : "Not installed")));
    if (engineHelp[engine.id]) item.append(make("p", "engine-help", `${engineHelp[engine.id]} Choose this reader from Engine when uploading or retrying.`));
    if (["paddleocr", "docling"].includes(engine.id)) item.append(make("p", "engine-quality-target", localQualityTarget));
    if (engine.version) item.append(make("small", "", `Version ${engine.version}`));
    list.append(item);
  });
}

function renderConnections() {
  const connections = app.state?.connections || {};
  Object.entries(connections).forEach(([provider, connected]) => {
    const badge = $(`[data-connection-state="${provider}"]`);
    if (!badge) return;
    const apiProvider = ["openai", "anthropic", "azure"].includes(provider);
    badge.textContent = !connected ? "Not connected" : !apiProvider ? "Connected"
      : app.providerVerified[provider] === true ? "Verified" : app.providerVerified[provider] === false ? "Check connection" : "Key saved";
    badge.classList.toggle("connected", connected && (!apiProvider || app.providerVerified[provider] === true));
  });
  const account = $("#chatgpt-account");
  account.replaceChildren(new Option("No connected accounts", ""));
  (app.state?.accounts || []).forEach((entry) => {
    const option = new Option(`${entry.email}${entry.connected ? "" : " (disconnected)"}`, entry.id);
    option.disabled = !entry.connected;
    option.selected = entry.id === app.state.active_account;
    account.add(option);
  });
  const hasAccount = Boolean(account.value);
  $("#remove-chatgpt").disabled = !hasAccount;
  $("#remove-chatgpt-cloud").disabled = !hasAccount;
  $("#export-chatgpt").disabled = !hasAccount;
  const cloud = app.authMode === "cloud";
  $$('[data-mode-only]').forEach((element) => { element.hidden = element.dataset.modeOnly !== (cloud ? "cloud" : "local"); });
  const azureEndpoint = $("[data-azure-endpoint]");
  azureEndpoint.textContent = app.state?.azure_endpoint ? `Connected endpoint: ${app.state.azure_endpoint}` : "";
  azureEndpoint.hidden = !app.state?.azure_endpoint;
  $("#connect-chatgpt").disabled = cloud;
  $("#chatgpt-account").disabled = false;
  ["settings-provider", "upload-provider", "retry-provider"].forEach((id) => {
    const select = $(`#${id}`);
    ["chatgpt", "claude_local"].forEach((value) => {
      const option = $(`option[value="${value}"]`, select);
      if (option) option.disabled = false;
    });
  });
}

function applySettings() {
  const settings = offeredAISettings(app.state?.settings);
  $("#settings-provider").value = settings.provider;
  $("#settings-model").value = settings.model;
  $("#settings-ai-fallback").checked = settings.ai_fallback !== false;
  syncModelPicker("settings");
  if (!$("#upload-dialog").open) {
    $("#upload-provider").value = settings.provider;
    $("#upload-model").value = settings.model;
    $("#upload-ai-fallback").checked = settings.ai_fallback !== false;
    syncModelPicker("upload");
  }
}

async function importFile(input, endpoint, successMessage) {
  if (!input.files[0]) return;
  const form = new FormData();
  form.append("file", input.files[0], input.files[0].name);
  await api(endpoint, { method: "POST", body: form });
  input.value = "";
  await loadState();
  notify(successMessage);
}

async function savePolicy(event) {
  event.preventDefault();
  const form = event.currentTarget;
  let decimals;
  try { decimals = JSON.parse(form.elements.currency_decimals.value); }
  catch (_) { notify("Currency decimals must be a valid JSON object.", "error"); return; }
  try {
    await api("/api/policy", { method: "POST", body: { price_tolerance: form.elements.price_tolerance.value, total_tolerance: form.elements.total_tolerance.value, currency_decimals: decimals } });
    await loadState();
    notify("Validation policy saved. Unexported invoices require a new evidence review.");
  } catch (error) { notify(error.message, "error", 7000); }
}

async function saveProviderSettings(event) {
  event.preventDefault();
  try {
    const body = { provider: $("#settings-provider").value, model: $("#settings-model").value.trim(), ai_fallback: $("#settings-ai-fallback").checked };
    await api("/api/settings", { method: "POST", body });
    await loadState();
    notify("Processing defaults saved.");
  } catch (error) { notify(error.message, "error"); }
}

async function fetchModels() {
  const button = $("#fetch-models");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    await loadProviderModels("settings", { force: true });
  }
  finally { button.disabled = false; button.textContent = "Load available models"; }
}

async function saveAzureConnection(form) {
  const endpoint = form.elements.endpoint.value.trim();
  const apiKey = form.elements.api_key.value;
  form.elements.api_key.value = "";
  try {
    await api("/api/connections/azure", { method: "POST", body: { endpoint, api_key: apiKey } });
    delete app.providerModels.azure;
    delete app.providerVerified.azure;
    await loadState();
    if ($("#settings-provider").value === "azure") loadProviderModels("settings", { force: true });
    notify("Azure AI Foundry connection saved on the server.");
  } catch (error) { notify(error.message, "error", 7000); }
}

async function saveApiKey(form) {
  const provider = form.dataset.provider;
  const input = form.elements.api_key;
  const apiKey = input.value;
  input.value = "";
  try {
    await api(`/api/connections/${provider}`, { method: "POST", body: { api_key: apiKey } });
    delete app.providerModels[provider];
    delete app.providerVerified[provider];
    await loadState();
    if ($("#settings-provider").value === provider) loadProviderModels("settings", { force: true });
    notify(`${humanize(provider)} connection saved on the server.`);
  } catch (error) { notify(error.message, "error", 7000); }
}

async function removeApiConnection(provider) {
  try {
    await api(`/api/connections/${provider}`, { method: "DELETE" });
    delete app.providerModels[provider];
    delete app.providerVerified[provider];
    await loadState();
    notify(`${humanize(provider)} connection removed.`);
  } catch (error) { notify(error.message, "error"); }
}

async function connectChatGPT() {
  try {
    const result = await api("/api/chatgpt/start", { method: "POST", body: { account_id: $("#chatgpt-account").value || null } });
    window.location.assign(result.url);
  } catch (error) { notify(error.message, "error", 7000); }
}

async function exportChatGPT() {
  const id = $("#chatgpt-account").value;
  if (!id) return;
  const button = $("#export-chatgpt");
  button.disabled = true;
  try {
    const result = await api("/api/chatgpt/export", { method: "POST", body: { id } });
    saveBlob(new Blob([JSON.stringify(result.bundle)], { type: "application/json" }), "invoice-studio-chatgpt-credentials.json");
    window.prompt("Passphrase for the downloaded file. It is shown only this once: copy it now and keep it apart from the file.", result.passphrase);
    await loadState();
    notify("ChatGPT connection moved out of this installation. Import the encrypted file with its passphrase into your hosted workspace, then delete it.");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; }
}

async function importChatGPT(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.bundle_file.files[0];
  const raw = file ? await file.text() : form.elements.bundle_json.value.trim();
  let bundle;
  try { bundle = JSON.parse(raw); }
  catch (_) { notify("Choose or paste a valid Invoice Studio registration JSON file.", "error"); return; }
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    await api("/api/chatgpt/import", { method: "POST", body: { bundle, passphrase: form.elements.passphrase.value } });
    form.reset();
    await loadState();
    notify("ChatGPT subscription registration imported and stored securely.");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; }
}

async function importClaudeSubscription(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const setupToken = form.elements.setup_token.value;
  form.elements.setup_token.value = "";
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    await api("/api/subscriptions/claude/import", { method: "POST", body: { setup_token: setupToken } });
    await loadState();
    notify("Claude subscription token stored securely. The connection is ready when the Claude CLI is installed.");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; }
}

async function removeClaudeSubscription() {
  try {
    await api("/api/subscriptions/claude", { method: "DELETE" });
    await loadState();
    notify("Claude subscription connection removed.");
  } catch (error) { notify(error.message, "error"); }
}

async function selectChatGPTAccount() {
  const id = $("#chatgpt-account").value;
  $("#remove-chatgpt").disabled = !id;
  $("#remove-chatgpt-cloud").disabled = !id;
  $("#export-chatgpt").disabled = !id;
  if (!id) return;
  try { await api("/api/chatgpt/select", { method: "POST", body: { id } }); await loadState(); notify("Active ChatGPT account changed."); }
  catch (error) { notify(error.message, "error"); }
}

async function removeChatGPT() {
  const id = $("#chatgpt-account").value;
  if (!id) return;
  try { await api(`/api/chatgpt/${encodeURIComponent(id)}`, { method: "DELETE" }); await loadState(); notify("ChatGPT account removed."); }
  catch (error) { notify(error.message, "error"); }
}

async function loadAudit() {
  if (!app.currentJob) return;
  const list = $("#audit-list");
  list.replaceChildren(make("li", "", "Loading audit entries…"));
  try {
    const entries = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/audit`);
    list.replaceChildren();
    if (!entries.length) list.append(make("li", "", "No audit entries yet."));
    entries.forEach((entry) => list.append(make("li", "", `${formatDate(entry.at)} · ${auditEventLabel(entry.event)}`)));
  } catch (error) { list.replaceChildren(make("li", "", error.message)); }
}

async function submitRetry(event) {
  event.preventDefault();
  if (!app.currentJob) return;
  const options = processingOptions("retry");
  if (options.engine === "ai" && !providerReady(options.provider, options.model)) {
    $("#retry-model-status").textContent = "Connect this provider and select a model before running AI extraction.";
    return;
  }
  try {
    await requestPreflight({
      kind: "retry",
      options: processingOptions("retry"),
      files: [{ name: app.currentJob.filename, size: app.currentJob.size }],
    });
  } catch (error) { notify(error.message, "error", 7000); }
}

async function runRetry(preflightToken, options) {
  const job = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/retry`, { method: "POST", body: { options, preflight_token: preflightToken } });
  app.currentJob = job;
  replaceJobSummary(job);
  renderSelectedJob(job);
  startPollingIfNeeded();
  notify("Invoice queued for reprocessing.");
}

function bindEvents() {
  applySidebarMode();
  window.matchMedia(COMPACT_SIDEBAR_QUERY).addEventListener("change", applySidebarMode);
  $("#sidebar-toggle").addEventListener("click", () => {
    rememberLayoutPreference(SIDEBAR_PREFERENCE, $("#app-shell").classList.contains("nav-compact") ? "expanded" : "compact");
    applySidebarMode();
  });
  setupPaneResizers();
  ["settings", "upload", "retry"].forEach(setupModelPicker);
  $$('button[value="cancel"]').forEach((button) => button.addEventListener("click", () => {
    button.closest("dialog")?.close("cancel");
  }));
  $$('[data-nav]').forEach((control) => control.addEventListener("click", (event) => { event.preventDefault(); navigate(control.dataset.nav); }));
  $$('[data-open-upload]').forEach((button) => button.addEventListener("click", openUpload));
  $$("[data-accuracy-period]").forEach((button) => button.addEventListener("click", () => {
    app.accuracyPeriod = button.dataset.accuracyPeriod;
    loadAccuracy().catch((error) => notify(error.message, "error"));
  }));
  $("#accuracy-supplier").addEventListener("change", () => loadAccuracy().catch((error) => notify(error.message, "error")));
  $("#job-search").addEventListener("input", renderJobs);
  $$("[data-inbox-sort]").forEach((button) => button.addEventListener("click", () => {
    app.inboxSort = button.dataset.inboxSort;
    renderJobs();
    $("#job-list").scrollTop = 0;
  }));
  $("#load-demo").addEventListener("click", prepareDemo);
  $("#add-line").addEventListener("click", () => { addLine(); markReviewDirty(); });
  $("#invoice-form").addEventListener("input", markReviewDirty);
  $("#confirm-review").addEventListener("change", markReviewDirty);
  $("#save-review").addEventListener("click", () => submitReview());
  $("#export-job").addEventListener("click", exportCurrentJob);
  $("#batch-review").addEventListener("click", openBatchReview);
  $("#confirm-batch-review").addEventListener("click", downloadBatchReview);
  $("#batch-export").addEventListener("click", exportBatch);
  $("#batch-draft").addEventListener("click", openBatchDraft);
  $("#confirm-batch-draft").addEventListener("click", downloadBatchDraft);
  $("#batch-fine-rules").addEventListener("click", openFineRules);
  $("#fine-rules-review").addEventListener("click", () => downloadFineRules("review"));
  $("#fine-rules-target").addEventListener("click", () => downloadFineRules("target"));
  $("#batch-delete").addEventListener("click", openDeleteInvoices);
  $("#confirm-delete-invoices").addEventListener("click", deleteInvoices);
  $("#delete-invoice-acknowledge").addEventListener("change", () => {
    $("#confirm-delete-invoices").disabled = !$("#delete-invoice-acknowledge").checked;
  });
  $("#select-all-jobs").addEventListener("change", (event) => {
    visibleSelectableJobs().forEach((job) => (event.target.checked ? app.selectedForBatch.add(job.id) : app.selectedForBatch.delete(job.id)));
    renderJobs();
  });
  $("#select-all-finished").addEventListener("click", () => {
    const finished = app.state.jobs.filter(isSelectableInvoice);
    const all = finished.length && finished.every((job) => app.selectedForBatch.has(job.id));
    app.selectedForBatch = all ? new Set() : new Set(finished.map((job) => job.id));
    renderJobs();
  });
  $("#select-processed").addEventListener("click", () => {
    const processed = app.state.jobs.filter(isBatchReviewable);
    const all = processed.length && app.selectedForBatch.size === processed.length
      && processed.every((job) => app.selectedForBatch.has(job.id));
    app.selectedForBatch = all ? new Set() : new Set(processed.map((job) => job.id));
    renderJobs();
  });
  $("#select-ready").addEventListener("click", () => {
    const ready = app.state.jobs.filter(isBatchReady);
    const all = ready.length && app.selectedForBatch.size === ready.length
      && ready.every((job) => app.selectedForBatch.has(job.id));
    app.selectedForBatch = all ? new Set() : new Set(ready.map((job) => job.id));
    renderJobs();
  });

  const fileInput = $("#invoice-files");
  fileInput.addEventListener("change", () => { queueFiles(fileInput.files); fileInput.value = ""; });
  const dropZone = $("#drop-zone");
  ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragover"); }));
  dropZone.addEventListener("drop", (event) => queueFiles(event.dataTransfer.files));
  ["#upload-ai-fallback", "#upload-engine", "#upload-provider", "#upload-model"].forEach((selector) => $(selector).addEventListener("input", updateUploadReadiness));
  ["upload", "retry"].forEach((prefix) => {
    $(`#${prefix}-prefer-native-text`).addEventListener("change", (event) => {
      setNativeTextPreference(event.currentTarget.checked);
    });
  });
  $("#upload-provider").addEventListener("change", () => loadProviderModels("upload", { providerChanged: true }));
  $("#upload-form").addEventListener("submit", beginUploadPreflight);

  $("#retry-job").addEventListener("click", () => {
    const rawOptions = app.currentJob?.options || app.state.settings;
    const options = { ...rawOptions, ...offeredAISettings(rawOptions) };
    // A job read with Engine "AI provider" re-reads with Auto and AI fallback: local readers first, then the AI for
    // what they leave open. The reviewer can still choose AI provider here.
    const aiOnly = options.engine === "ai";
    $("#retry-engine").value = aiOnly ? "auto" : options.engine || "auto";
    $("#retry-language").value = options.language || "en";
    const preferNativeText = app.nativeTextPreferenceExplicit
      ? app.preferNativeText
      : options.prefer_native_text !== false;
    setNativeTextPreference(preferNativeText, { remember: false });
    $("#retry-ai-fallback").checked = aiOnly || options.ai_fallback !== false;
    $("#retry-provider").value = options.provider || "openai";
    $("#retry-model").value = options.model || "";
    $("#retry-dialog").showModal();
    loadProviderModels("retry");
  });
  $("#retry-form").addEventListener("submit", submitRetry);
  $("#preflight-form").addEventListener("submit", confirmPreflight);
  const closePreflight = () => { $("#preflight-dialog").close("cancel"); cancelPreflight(); };
  $("#preflight-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closePreflight(); });
  $("#cancel-preflight").addEventListener("click", closePreflight);
  $("#cancel-preflight-x").addEventListener("click", closePreflight);
  $("#empty-configure-ai").addEventListener("click", () => navigate("engines"));
  $("#empty-retry").addEventListener("click", () => $("#retry-job").click());
  $("#empty-show-text").addEventListener("click", () => {
    const text = $("#extracted-text-wrap");
    text.open = true;
    text.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $$('[data-open-manual-draft]').forEach((button) => button.addEventListener("click", openManualDraft));
  $("#add-manual-draft-line").addEventListener("click", () => addManualDraftLine());
  $("#manual-draft-form").addEventListener("submit", downloadManualDraft);
  const closeManualDraft = () => $("#manual-draft-dialog").close();
  $("#cancel-manual-draft").addEventListener("click", closeManualDraft);
  $("#close-manual-draft-x").addEventListener("click", closeManualDraft);
  $("#manual-draft-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeManualDraft(); });
  $("#export-extraction-draft").addEventListener("click", openExtractionDraft);
  $("#confirm-extraction-draft").addEventListener("click", downloadExtractionDraft);

  $(".trace-card").addEventListener("toggle", (event) => { if (event.currentTarget.open) loadAudit(); });
  $("#reference-file").addEventListener("change", (event) => { $("#reference-file-name").textContent = event.target.files[0]?.name || "No file chosen"; $("#upload-reference").disabled = !event.target.files[0]; });
  $("#upload-reference").addEventListener("click", async () => {
    try { await importFile($("#reference-file"), "/api/references", "References imported. Unexported invoices require a new review."); $("#reference-file-name").textContent = "No file chosen"; $("#upload-reference").disabled = true; }
    catch (error) { notify(error.message, "error", 7000); }
  });
  $("#load-demo-references").addEventListener("click", async () => {
    try { await api("/api/references/demo", { method: "POST", body: {} }); await loadState(); notify("Synthetic reference data loaded. It is for demonstration only."); }
    catch (error) { notify(error.message, "error"); }
  });
  $("#reference-lookup-form").addEventListener("submit", (event) => { event.preventDefault(); searchReferenceLookup(); });
  $("#reference-lookup-more").addEventListener("click", () => searchReferenceLookup({ append: true }));
  $("#template-file").addEventListener("change", (event) => { $("#template-file-name").textContent = event.target.files[0]?.name || "No file chosen"; $("#upload-template").disabled = !event.target.files[0]; });
  $("#upload-template").addEventListener("click", async () => {
    try { await importFile($("#template-file"), "/api/templates", "Supplier template registered."); $("#template-file-name").textContent = "No file chosen"; $("#upload-template").disabled = true; }
    catch (error) { notify(error.message, "error", 7000); }
  });
  $("#policy-form").addEventListener("submit", savePolicy);
  $("#mapping-upload-form").addEventListener("submit", validateMappingUpload);
  $("#mapping-file").addEventListener("change", () => {
    mappingUploadId = null; $("#mapping-activate").hidden = true; $("#mapping-report").replaceChildren();
  });
  $("#mapping-activate").addEventListener("click", () => {
    if (mappingUploadId) changeMappingVersion(`/api/mapping-tables/uploads/${mappingUploadId}/activate`, $("#mapping-activate"));
  });
  $("#mapping-download").addEventListener("click", () => {
    downloadApi("/api/mapping-tables/active/workbook", "mapping-tables.xlsx").catch((error) => notify(error.message, "error"));
  });
  $("#provider-settings-form").addEventListener("submit", saveProviderSettings);
  $("#settings-provider").addEventListener("change", () => loadProviderModels("settings", { providerChanged: true }));
  $("#retry-provider").addEventListener("change", () => loadProviderModels("retry", { providerChanged: true }));
  $("#fetch-models").addEventListener("click", fetchModels);
  $("#model-list").addEventListener("change", (event) => { if (event.target.value) $("#settings-model").value = event.target.value; });
  $$(".api-key-form").forEach((form) => {
    const save = form.dataset.provider === "azure" ? saveAzureConnection : saveApiKey;
    form.addEventListener("submit", (event) => { event.preventDefault(); save(form); });
    $(".remove-connection", form).addEventListener("click", () => removeApiConnection(form.dataset.provider));
  });
  $("#connect-chatgpt").addEventListener("click", connectChatGPT);
  $("#export-chatgpt").addEventListener("click", exportChatGPT);
  $("#chatgpt-import-form").addEventListener("submit", importChatGPT);
  $("#chatgpt-account").addEventListener("change", selectChatGPTAccount);
  $("#remove-chatgpt").addEventListener("click", removeChatGPT);
  $("#remove-chatgpt-cloud").addEventListener("click", removeChatGPT);
  $("#claude-subscription-form").addEventListener("submit", importClaudeSubscription);
  $("#remove-claude-subscription").addEventListener("click", removeClaudeSubscription);
  $$('[data-api-download]').forEach((link) => link.addEventListener("click", async (event) => {
    event.preventDefault();
    try { await downloadApi(new URL(link.href).pathname, link.dataset.apiDownload || "download"); }
    catch (error) { notify(error.message, "error", 7000); }
  }));
  $("#microsoft-sign-in").addEventListener("click", signInMicrosoft);
  $("#auth-retry").addEventListener("click", initializeAccess);
  $("#auth-sign-out").addEventListener("click", signOutCloud);
  $("#cloud-sign-out").addEventListener("click", signOutCloud);
}

function showAuthGate({ message, detail = "", busy = false, signIn = false, retry = false, signOut = false }) {
  $("#app-shell").hidden = true;
  $("#auth-gate").hidden = false;
  $("#auth-message").textContent = message;
  $("#auth-spinner").hidden = !busy;
  $("#microsoft-sign-in").hidden = !signIn;
  $("#auth-retry").hidden = !retry;
  $("#auth-sign-out").hidden = !signOut;
  $("#auth-detail").hidden = !detail;
  $("#auth-detail").textContent = detail;
}

function cloudIdentity(session) {
  const email = String(session.email || "Verified user");
  $("#cloud-user-email").textContent = email;
  $("#cloud-user-avatar").textContent = email.slice(0, 1).toUpperCase();
  $("#cloud-user").hidden = false;
  $("#runtime-badge > span:last-child").textContent = "Cloud workspace";
  $$("[data-admin-only]").forEach((element) => { element.hidden = session.role !== "admin"; });
}

async function startWorkspace() {
  const params = new URLSearchParams(location.search);
  if (params.get("connected") === "chatgpt") notify("ChatGPT account connected.");
  if (params.get("connection_error")) notify(params.get("connection_error"), "error", 8000);
  if (params.size) history.replaceState(null, "", location.pathname + (location.hash || ""));
  try {
    await loadState();
    const route = location.hash.slice(1);
    const routes = ["workspace", "references", "engines", "accuracy"];
    if (app.authMode === "cloud" && app.sessionUser?.role === "admin") routes.push("users");
    navigate(routes.includes(route) ? route : "workspace");
    if (app.state.jobs.length) await selectJob(sortJobsByAdded(app.state.jobs)[0].id);
    $("#auth-gate").hidden = true;
    $("#app-shell").hidden = false;
    app.workspaceStarted = true;
  } catch (error) {
    showAuthGate({ message: "The workspace could not be loaded.", detail: error.message, retry: true, signOut: app.authMode === "cloud" });
  }
}

async function verifyCloudSession() {
  showAuthGate({ message: "Verifying workspace access…", busy: true });
  try {
    const session = await api("/api/session");
    if (!session?.email || !session?.uid) throw new Error("The session response was incomplete.");
    app.sessionUser = session;
    app.sessionVerified = true;
    cloudIdentity(session);
    await startWorkspace();
  } catch (error) {
    app.sessionVerified = false;
    // 403: Entra let the person in but gave them no Invoice Studio role. Anything else: sign in again.
    if (error?.status === 403) {
      showAuthGate({ message: "This account does not have workspace access.", detail: `${error.message}. Ask your administrator to add you to the Invoice Studio group.`, signOut: true });
    } else {
      showAuthGate({ message: "Sign in with your work account to continue.", detail: error?.message || "Sign-in could not be verified. Sign in again.", signIn: true, signOut: true });
    }
  }
}

// An idle server takes up to about 80 s to start. Until it is up, the platform answers 429 or a 5xx:
// those mean "starting", not "no access".
const SERVER_STARTING_STATUSES = new Set([429, 500, 502, 503, 504]);

async function waitForServer(limitMs = 150000) {
  const started = Date.now();
  let delay = 1000;
  for (;;) {
    let status = 0;
    try {
      status = (await fetch("/api/public-config", { headers: { Accept: "application/json" }, cache: "no-store" })).status;
    } catch (_) {
      status = 0;
    }
    if (status && !SERVER_STARTING_STATUSES.has(status)) return;
    if (Date.now() - started + delay > limitMs) return;
    showAuthGate({
      message: "Starting the workspace…",
      detail: "The server was idle and is starting up. This can take up to a minute.",
      busy: true,
    });
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 8000);
  }
}

async function initializeAccess() {
  showAuthGate({ message: "Checking workspace access…", busy: true });
  try {
    if (!window.InvoiceStudioAuth) throw new Error("The sign-in module did not load.");
    await waitForServer();
    showAuthGate({ message: "Checking workspace access…", busy: true });
    const runtime = await window.InvoiceStudioAuth.bootstrap();
    if (!runtime.cloud) {
      app.authMode = "local";
      app.sessionVerified = true;
      $("#runtime-badge > span:last-child").textContent = "Local workspace";
      $("#cloud-user").hidden = true;
      await startWorkspace();
      return;
    }
    app.authMode = "cloud";
    if (runtime.user) {
      await verifyCloudSession();
      return;
    }
    showAuthGate({ message: "Sign in with your work account to continue.", detail: runtime.error || "", signIn: true, signOut: Boolean(runtime.error) });
  } catch (_) {
    app.authMode = "unknown";
    app.sessionVerified = false;
    showAuthGate({ message: "Workspace access could not be verified.", detail: "Check the connection and retry. Local mode is used only when the server explicitly enables it.", retry: true });
  }
}

// The page leaves for Microsoft and comes back to "/", where bootstrap() picks up the result.
async function signInMicrosoft() {
  const button = $("#microsoft-sign-in");
  button.disabled = true;
  try {
    await window.InvoiceStudioAuth.signIn();
  } catch (_) {
    showAuthGate({ message: "Sign in with your work account to continue.", detail: "Microsoft sign-in could not be started. Try again.", signIn: true });
  } finally {
    button.disabled = false;
  }
}

async function signOutCloud() {
  try { await window.InvoiceStudioAuth.signOut(); } catch (_) { /* The local UI still locks. */ }
  app.sessionVerified = false;
  app.sessionUser = null;
  clearInterval(app.pollTimer);
  app.pollTimer = null;
  clearSelectedJob();
  $("#cloud-user").hidden = true;
  showAuthGate({ message: "Signed out. Sign in with your work account to continue.", signIn: true });
}


// ---- Users (administrators only) ---------------------------------------------------------------------------
// Read-only: who has signed in and the role their token carried then. Access is managed in Microsoft Entra ID.

function formatWhen(iso) {
  if (!iso) return "Never";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}

function usersEmptyRow(text) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 5;
  cell.className = "table-empty";
  cell.textContent = text;
  row.append(cell);
  return row;
}

async function loadUsers() {
  if (app.authMode !== "cloud" || app.sessionUser?.role !== "admin") return;
  try {
    const data = await api("/api/users");
    app.users = Array.isArray(data.users) ? data.users : [];
    app.userGroups = data.groups || {};
    renderUsers();
  } catch (error) {
    $("#users-rows").replaceChildren(usersEmptyRow(error.message));
  }
}

function renderUsers() {
  const groups = app.userGroups || {};
  const admins = groups.admin ? `“${groups.admin}”` : "the Invoice Studio administrator group";
  const members = groups.member ? `“${groups.member}”` : "member group";
  $("#users-intro").textContent = `People sign in with their work account. Access and roles are managed in Microsoft Entra ID: add someone to ${admins} or ${members}, and remove them there to take access away. This list shows who has signed in here and the role they had at their last sign-in. It does not grant or remove access.`;
  const users = app.users || [];
  const rows = $("#users-rows");
  if (!users.length) {
    rows.replaceChildren(usersEmptyRow("Nobody has signed in yet."));
    return;
  }
  const text = (value) => { const cell = document.createElement("td"); cell.textContent = value; return cell; };
  rows.replaceChildren(...users.map((user) => {
    const me = user.oid === app.sessionUser?.uid;
    const row = document.createElement("tr");
    row.append(
      text((user.name || "—") + (me ? " (you)" : "")),
      text(user.email || "—"),
      text(user.role === "admin" ? "Administrator" : "Member"),
      text(formatWhen(user.first_login_at)),
      text(formatWhen(user.last_login_at)),
    );
    return row;
  }));
}

async function init() {
  bindEvents();
  await initializeAccess();
}

document.addEventListener("DOMContentLoaded", init);
