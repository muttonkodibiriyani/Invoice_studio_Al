// Minimal DOM/SDK doubles: execute the actual browser scripts without network or dependencies.
// This checks script behavior, not browser layout, CSP enforcement or the live identity provider.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function main() {
  const [script, scenario] = process.argv.slice(2);
  const nodes = new Map();
  class Element {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase();
      this.hidden = false;
      this.value = "";
      this.textContent = "";
      this.children = [];
      this.dataset = {};
      this.listeners = {};
      this.attributes = {};
      this.classList = { toggle: (name, on) => { this.className = on ? `${this.className} ${name}` : this.className; } };
    }
    set innerHTML(_) { throw Error("unsafe HTML sink"); }
    set outerHTML(_) { throw Error("unsafe HTML sink"); }
    insertAdjacentHTML() { throw Error("unsafe HTML sink"); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    add(option) { this.append(option); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    async emit(type) {
      for (const handler of this.listeners[type] || []) {
        await handler({ preventDefault() {}, currentTarget: this });
      }
    }
    focus() { this.focused = true; document.activeElement = this; }
    select() { this.selected = true; }
    showModal() { this.open = true; }
    async close() { this.open = false; await this.emit("close"); }
    remove() { this.removed = true; }
  }
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, new Element());
    return nodes.get(selector);
  };
  const document = {
    querySelector: node, querySelectorAll: () => [],
    createElement: (tag) => new Element(tag),
    addEventListener: (type, handler) => { document[type] = handler; },
  };
  const forbiddenStorage = new Proxy({}, { get() { throw Error("storage touched"); }, set() { throw Error("storage touched"); } });
  const location = { pathname: "/", hash: "", search: "", origin: "https://studio.example.test" };
  const replacements = [];
  const delays = [];
  const hostileEmail = '<img src=x onerror="alert(1)">@example.test';
  // A scripted double of entra-auth.bundle.js: the real MSAL redirect is not run here.
  const signIns = [];
  const signOuts = [];
  const sdk = {
    runtime: { cloud: true, user: null, error: "" },
    async bootstrap() { return sdk.runtime; },
    async getToken() { return "synthetic-token"; },
    async signIn() { signIns.push(location.href); },
    async signOut() { signOuts.push(true); },
    currentUser: () => sdk.runtime.user,
  };
  const window = { localStorage: forbiddenStorage, sessionStorage: forbiddenStorage, InvoiceStudioAuth: sdk };
  const context = vm.createContext({
    document, window, location, URLSearchParams,
    history: { replaceState(_, __, path) {
      replacements.push(path); location.hash = ""; location.search = "";
    } },
    fetch() { throw Error("unexpected server request"); },
    // Run backoff instantly but preserve ordering. Script load/error events run first.
    setTimeout(callback, delay) { return setImmediate(() => { delays.push(delay); callback(); }); },
    clearTimeout(timer) { clearImmediate(timer); },
    Option: function (label, value) { const option = new Element("option"); option.textContent = label; option.value = value; return option; },
  });
  if (scenario === "target-chip") {
    // The inbox chip says what the target sheet is. Cells still to check, cells still empty and a
    // failed row check are three different things, and "Target check failed" named none of them: on a
    // plain install with no owner catalog imported yet, every invoice showed it.
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const chip = (status, counts, failed, summary) => vm.runInContext(
      `targetChip(${JSON.stringify({ status, rules: { target_check: { counts, failed, summary } } })})`, context);
    const clean = { needs_checking: 0, empty_flagged: 0 };
    assert.equal(vm.runInContext("targetChip({status: 'review', rules: {}})", context), null, "no check, no chip");
    const cellStatus = (sub) => vm.runInContext(`targetCellStatus({status: "verified", sub: "${sub}"})`, context);
    assert.equal(cellStatus(""), "Verified");
    assert.equal(cellStatus("owner_entry"), "Verified (owner entry)");
    assert.equal(cellStatus("rule_derived"), "Verified (rule derived)");
    const waiting = chip("review", { needs_checking: 3, empty_flagged: 10 }, [], "sheet");
    assert(waiting.textContent === "3 to check" && waiting.className.includes("warning"));
    const empty = chip("review", { needs_checking: 0, empty_flagged: 10 }, [], "Target sheet: 2 verified");
    assert(empty.textContent === "10 cells empty", `no owner catalog imported reads as "${empty.textContent}"`);
    assert(empty.className.includes("warning") && empty.title === "Target sheet: 2 verified");
    assert(chip("review", { needs_checking: 0, empty_flagged: 1 }, []).textContent === "1 cell empty");
    const broke = chip("review", clean, ["lines_to_net"], "Target sheet: 12 verified");
    assert(broke.textContent === "1 row check failed", `a failed row check reads as "${broke.textContent}"`);
    assert(broke.title.includes("Lines To Net"), "the chip names the row check, never a value");
    assert(chip("review", clean, ["lines_to_net", "joins"]).textContent === "2 row checks failed");
    const verified = chip("review", clean, []);
    assert(verified.textContent === "Target verified" && verified.className.includes("success"));
    const confirmed = chip("ready", { needs_checking: 0, empty_flagged: 10 }, []);
    assert(confirmed.textContent === "Target confirmed" && confirmed.className.includes("success"));
    // A check stored before #41 has holds but no "failed" names: held by a row check, it is never green.
    const old = vm.runInContext(`targetChip(${JSON.stringify({ status: "review", rules: { target_check: { counts: clean, holds: true } } })})`, context);
    assert(old.textContent === "Held at review" && old.className.includes("warning"), `a held pre-#41 job reads as "${old.textContent}"`);
    return;
  }
  if (scenario === "export-confirm") {
    // Export Excel asks first only for a Blocked or blank invoice (D11: it asks, it never refuses). The count comes
    // from the server check, which reads the sheets the writer fills; a draft download shows nothing new.
    const source = fs.readFileSync(script, "utf8");
    vm.runInContext(source, context);
    // bindEvents needs a real page; the browser run covers it. Here, that it binds these two handlers.
    assert(source.includes('$("#confirm-export-anyway").addEventListener("click", writeCurrentExport)'));
    assert(source.includes('$("#confirm-batch-export").addEventListener("click", confirmBatchExport)'));
    let reply = { ready: [], blocked: [] };
    const calls = [];
    context.calls = calls;
    context.studioBackground = async (kind, body) => {
      calls.push(kind);
      return { ok: true, status: 200, json: async () => (kind === "export-batch" ? { id: "b1", url: "/api/batches/b1", count: body.jobs.length } : reply) };
    };
    context.api = async (path) => { calls.push(path); return { id: "e1234567", url: "/api/exports/e1" }; };
    context.studioFetch = async (path) => { calls.push(path); return { ok: true, status: 200, blob: async () => ({}), headers: new Map() }; };
    vm.runInContext(`
      downloadApi = async () => {}; loadState = async () => {}; selectJob = async () => {}; notify = () => {}; downloadBlob = () => {};
      updateBatchControls = () => {};
      $("#confirm-export-anyway").addEventListener("click", writeCurrentExport);
      $("#confirm-batch-export").addEventListener("click", confirmBatchExport);
      app.state = { jobs: [{ id: "j1", revision: 3, filename: "one.pdf", status: "review" }, { id: "j2", revision: 1, filename: "two.pdf", status: "review" }] };
      app.currentJob = app.state.jobs[0];
    `, context);
    const dialog = node("#export-confirm-dialog");
    const exportOne = () => vm.runInContext("exportCurrentJob()", context);
    const single = "/api/jobs/j1/export";
    const answer = (status, blank) => { reply = { ready: [{ id: "j1", filename: "one.pdf", revision: 3, transaction_number: 1, attention: [], status, blank_fields: blank }], blocked: [] }; calls.length = 0; dialog.open = false; };

    answer("Approved", 0);
    await exportOne();
    assert(!dialog.open && calls.includes(single), "a clean invoice exports with no extra step");
    answer("Review", 0);
    await exportOne();
    assert(!dialog.open && calls.includes(single), "no blanks and not Blocked is not asked about");

    answer("Blocked", 18);
    await exportOne();
    assert(dialog.open && !calls.includes(single), "a Blocked invoice is not exported before the answer");
    assert.equal(node("#export-confirm-text").textContent, "This invoice is Blocked and has 18 blank fields. Export anyway?");
    assert.equal(node("#export-job").textContent, "Export Excel");
    assert(!node("#export-job").disabled, "cancelling leaves the button usable");
    await node("#confirm-export-anyway").emit("click");
    assert(calls.includes(single), "Export anyway exports");
    assert(!dialog.open, "the dialog closes when the export starts");

    answer("Approved", 1);
    await exportOne();
    assert.equal(node("#export-confirm-text").textContent, "This invoice is Approved and has 1 blank field. Export anyway?");
    answer("Review", 2);
    await exportOne();
    assert.equal(node("#export-confirm-text").textContent, "This invoice is in Review and has 2 blank fields. Export anyway?");
    answer("Blocked", 0);
    await exportOne();
    assert.equal(node("#export-confirm-text").textContent, "This invoice is Blocked and has 0 blank fields. Export anyway?");

    // An already exported invoice downloads its file; nothing to ask, and no check.
    answer("Blocked", 9);
    vm.runInContext("app.currentJob = { ...app.currentJob, export_id: 'e1' }", context);
    await exportOne();
    assert(!dialog.open && calls.includes(single) && !calls.includes("export-check"), "Download Excel asks nothing");
    vm.runInContext("app.currentJob = app.state.jobs[0]", context);
    // An integrity refusal is the server's to word: the export runs and says why.
    reply = { ready: [], blocked: [{ id: "j1", reasons: [{ code: "Duplicate", message: "Duplicate" }] }] };
    calls.length = 0;
    await exportOne();
    assert(!dialog.open && calls.includes(single), "a blocked check still reaches the export's own refusal");

    // Batch: the existing dialog is the one step. A clean selection reads as it did; a Blocked or blank one names each.
    const batch = () => vm.runInContext("app.selectedForBatch = new Set(['j1', 'j2']); exportBatch()", context);
    const item = (id, filename, status, blank) => ({ id, filename, revision: 1, transaction_number: 1, attention: [], status, blank_fields: blank });
    const warning = node("#batch-export-warning");
    reply = { ready: [item("j1", "one.pdf", "Approved", 0), item("j2", "two.pdf", "Approved", 0)], blocked: [] };
    calls.length = 0;
    await batch();
    assert(warning.hidden && !calls.includes("export-batch") && node("#batch-export-dialog").open);
    assert.equal(node("#confirm-batch-export").textContent, "Export 2 invoices");
    reply = { ready: [item("j1", "one.pdf", "Blocked", 18), item("j2", "two.pdf", "Approved", 0)], blocked: [] };
    await batch();
    assert(!warning.hidden && !calls.includes("export-batch"));
    assert.deepEqual(node("#batch-export-warning-list").children.map((row) => row.textContent),
      ["one.pdf: This invoice is Blocked and has 18 blank fields."]);
    assert.equal(node("#confirm-batch-export").textContent, "Export 2 invoices anyway");
    await node("#confirm-batch-export").emit("click");
    assert(calls.includes("export-batch"), "Export anyway exports the batch");

    // A draft download asks nothing new and checks nothing.
    calls.length = 0;
    dialog.open = false;
    vm.runInContext("app.currentJob = { ...app.state.jobs[0], status: 'review' }", context);
    await vm.runInContext("downloadExtractionDraft()", context);
    assert(!dialog.open && !calls.includes("export-check"), "the extraction draft shows nothing new");
    return;
  }
  if (scenario === "reader-marks") {
    // Every value names its reader, an AI value is marked as the AI's, and the view says why the AI ran
    // and which local reader was not installed, with its install hint.
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const badge = (reader) => vm.runInContext(`readerBadge(${JSON.stringify(reader)})`, context);
    assert.equal(badge(undefined), null, "no reader, no badge");
    assert(badge("paddleocr").textContent === "PaddleOCR" && badge("native_pdf_text").textContent === "PDF text");
    assert(badge("ai").textContent === "AI" && badge("ai").className.includes("reader-ai") && badge("ai").title.includes("AI"));
    const texts = (box) => box.children.map((child) => child.textContent);
    const box = node("#ai-reader");
    const hint = "Not installed. Run ./scripts/setup.sh --ocr";
    context.job = { readers: { ai: { status: "gap_fill", calls: 1, need: "gap_fill",
      reason: "paddleocr: not installed. Run ./scripts/setup.sh --ocr. The local readers left gaps" },
      not_installed: [{ engine: "paddleocr", hint }, { engine: "docling", hint }] } };
    vm.runInContext("renderAiReader(job)", context);
    assert(!box.hidden && box.className.includes("ai-used"));
    assert.deepEqual(texts(box), ["AI reader: Gap fill · 1 call",
      "Why the AI ran: paddleocr: not installed. Run ./scripts/setup.sh --ocr. The local readers left gaps",
      `Docling: ${hint}`], "the hint already in the reason is not repeated; another missing reader still shows");
    context.job = { readers: { ai: { status: "skipped", calls: 0, reason: "The local readers read a complete, reconciled invoice; the AI cross-check is off" } } };
    vm.runInContext("renderAiReader(job)", context);
    assert(!box.className.includes("ai-used") && texts(box)[0] === "AI reader: Not needed · 0 calls" && texts(box)[1].startsWith("Why: "));
    context.job = { readers: { ai: { status: "not_read", calls: 1, reason: "Not read: the local OCR read is weak" } } };
    vm.runInContext("renderAiReader(job)", context);
    assert(texts(box)[0] === "AI reader: Read rejected · 1 call");
    vm.runInContext("renderAiReader({})", context);
    context.job = { readers: { ai: { status: "gap_fill", calls: 1, reason: "gaps",
      rules_applied: [{ line: 0, rule: "air-1", item_id: "1001", source: "Item master, items:12" }],
      suggestions: [{ line: 1, kind: "search", field: "item_id", value: "1002", source: "Item master, items:13", why: "similar" }] } } };
    vm.runInContext("renderAiReader(job)", context);
    assert(texts(box).some((t) => t.startsWith("Line 1: item 1001 filled by AI-added rule air-1")), "a rule's fill names the rule");
    assert(texts(box).some((t) => t.startsWith("Suggestion") && t.includes("1002") && t.includes("Item master, items:13")), "a suggestion names its source");
    assert(!fs.readFileSync(script, "utf8").match(/ai-rules[^\n]*approve|approve[^\n]*ai-rules/i), "AI-added rules have no approval step");
    vm.runInContext("renderAiReader({})", context);
    assert(box.hidden && !box.children.length, "a job with no readers record shows nothing");
    // The running view lists the stages in the order the server reads them: PDF text before a chosen OCR reader.
    const stages = (job) => vm.runInContext(`plannedProcessingStages(${JSON.stringify(job)})`, context);
    assert.deepEqual([...stages({ filename: "a.pdf", options: { engine: "paddleocr" }, progress: { engine: "native_pdf_text" } })], ["native_pdf_text", "paddleocr"]);
    assert.deepEqual([...stages({ filename: "a.png", options: { engine: "docling" } })], ["docling"]);
    assert.deepEqual([...stages({ filename: "a.pdf", options: { engine: "auto", ai_fallback: true, provider: "openai" } })],
      ["invoice2data", "paddleocr", "docling", "openai"]);
    return;
  }
  if (scenario === "possible-copy") {
    // A confirm does not clear this hold, so nothing on screen may ask for one while it is the thing left to do.
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const message = "Possible copy of a.pdf, exported 2026-11-06: the same file. Export anyway or delete this one.";
    const copy = { code: "Possible Duplicate", message, owner: "Accounts payable", line: null, blocking: true, level: "review" };
    const other = { code: "Blank Cell", message: "A cell is blank", owner: "", line: 1, blocking: true, level: "review" };
    const render = (issues) => {
      context.job = { id: "job-1", status: "review", validation: { source: "fine_rules", ready: false, issues } };
      vm.runInContext("renderValidation(job)", context);
      const details = node("#validation-summary").children[0];
      return { verdict: details.children[0].children[0].textContent, text: details.children.map((el) => el.textContent).filter(Boolean),
               items: details.children.filter((el) => el.tagName === "UL").map((ul) => ul.children.map((li) => li.textContent)) };
    };
    const alone = render([copy]);
    assert.equal(alone.verdict, message);
    assert(alone.text.includes("Possible copy: export anyway or delete (1)"));
    assert(!alone.text.some((line) => /confirm review/.test(line)));
    const unconfirmed = render([copy, { code: "REVIEW", message: "Compare, then confirm review", blocking: true, level: "review" }]);
    assert.equal(unconfirmed.verdict, message);
    assert(!unconfirmed.text.some((line) => /Last step/.test(line)));
    const mixed = render([copy, other]);
    assert.equal(mixed.verdict, message);
    assert.deepEqual(mixed.text.filter((line) => /\(\d+\)$/.test(line)), ["Possible copy: export anyway or delete (1)", "Check, then confirm review (1)"]);
    assert(mixed.items[0][0].includes(message), "the hold is listed first");
    context.validation = { ready: false, issues: [copy] };
    const toast = vm.runInContext("savedMessage(validation)", context);
    assert.equal(toast, `Saved. ${message}`);
    assert(!/Resolve the listed/.test(toast));
    context.validation = { ready: false, issues: [other] };
    assert(/Resolve the listed/.test(vm.runInContext("savedMessage(validation)", context)), "other holds keep their words");
    return;
  }
  if (scenario === "review-prompts") {
    // The real payloads: rules.coverage (#604a), Item cell lookup_trace (#23, rules_view) and the rules_view header
    // fields location_type / location (#27), held when value is null. One prompt per problem, the evidence only
    // behind Details, and Update item master posts the refresh then polls it until done (#28).
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const trace = { state: "Ambiguous", rule: "ALG-016", reason: "VPN matches more than one item",
      printed: { barcode: [], vpn: ["ABC-12"], description: "Tea" },
      searched: { columns: ["VPN"], supplier_sites: ["S100"] }, item_master_version: "v7",
      rows: ["1002345", "1002346"], rows_total: 2, manual_fix: null };
    const ok = (value) => ({ value, evidence: [{ kind: "table", source: "item master", reference: "row 4" }] });
    const job = { id: "job-1", status: "review", validation: { source: "fine_rules" }, rules: {
      item_lines: { resolved: 8, total: 10, rate: 0.8 },
      coverage: { matched: 9, total: 10, pct: "90.00", tier: "Second fallback", export: "after_fill", low: true, prompt: "P-COV",
        missing: [{ line: 2, filled: false }] },
      fields: {
        location_type: { label: "Location type", target: "Location Type", value: null, evidence: [], flagged: true, reason: "Not found" },
        location: { label: "Location", target: "Location", value: null, evidence: [], flagged: true, reason: "Not found" },
      },
      lines: [
        { line: 1, cells: { Item: ok("1000001") } },
        { line: 2, cells: { Item: { value: null, evidence: [], flagged: true, reason: "Not found", lookup_trace: trace } } },
        { line: 3, cells: { Item: { value: null, evidence: [], flagged: true, reason: "Not found", lookup_trace: {
          state: "Not in item master", rule: "R-003", reason: "No item master row", printed: { barcode: ["00012345678936"],
            vpn: ["ZZ-9"], description: "Widget" }, searched: { columns: ["BARCODE", "VPN", "ITEM_DESC"], supplier_sites: ["S100"] },
          item_master_version: "v7", rows: [], rows_total: 0, manual_fix: null } } } },
      ] } };
    context.job = job;
    vm.runInContext("app.currentJob = job; renderRulesResult(job)", context);
    assert(!node("#coverage-banner").hidden && node("#rules-item-rate").hidden, "coverage replaces the old item rate");
    assert.equal(node("#coverage-text").textContent, "Low coverage 9 of 10 lines (90%) · Second fallback · fill or confirm the lines below before export");
    assert(!node("#update-item-master").hidden);
    const problems = node("#rules-problems");
    assert(!problems.hidden);
    assert.deepEqual(problems.children.map((li) => li.children[0].textContent), [
      "Location type · Store or Warehouse?", "Location number is blank in the item master · add it",
      "Line 2 “ABC-12” · Ambiguous · is it 1002345?", "Line 3 “ZZ-9” · Not in item master"]);
    const walk = (el) => [el, ...(el.children || []).flatMap(walk)];
    const find = (root, pred) => walk(root).find(pred);
    const button = (root, label) => find(root, (el) => el.tagName === "BUTTON" && el.textContent === label);
    const [typeLi, locationLi, ambiguous, missing] = problems.children;
    // Location type: the pick writes the header entry, which collectEntries sends.
    assert(button(typeLi, "Store (S)"));
    await button(typeLi, "Warehouse (W)").emit("click");
    const typeEntry = find(node("#rules-fields"), (el) => el.dataset?.name === "location_type");
    assert.equal(typeEntry.value, "Warehouse (W)");
    assert.equal(button(typeLi, "Warehouse (W)").attributes["aria-pressed"], "true");
    await button(locationLi, "Add location").emit("click");
    assert(find(node("#rules-fields"), (el) => el.dataset?.name === "location").focused, "Add location focuses the location entry");
    // Line 2: Yes takes the first row; Pick another offers the other rows; the trace stays behind Details.
    const entry = find(ambiguous, (el) => el.className === "rules-entry");
    assert(entry.hidden && entry.attributes["aria-label"] === "Item line 2, entered by reviewer");
    await button(ambiguous, "Yes").emit("click");
    assert(entry.value === "1002345" && !entry.hidden);
    await button(ambiguous, "Pick another").emit("click");
    const pick = find(ambiguous, (el) => el.tagName === "SELECT");
    assert(!pick.hidden && pick.focused && pick.children.length === 3);
    pick.value = "1002346"; await pick.emit("change");
    assert.equal(entry.value, "1002346");
    const details = find(ambiguous, (el) => el.tagName === "DETAILS");
    assert.equal(details.children[0].textContent, "Details");
    const facts = details.children[1].children.map((el) => el.textContent);
    assert.deepEqual(facts, ["Rule", "ALG-016 · VPN matches more than one item", "Printed VPN", "ABC-12",
      "Printed description", "Tea", "Columns searched", "VPN", "Supplier sites", "S100", "Item master version", "v7",
      "Rows returned", "1002345, 1002346"]);
    trace.rows = ["1", "2", "3", "4", "5"]; trace.rows_total = 9; trace.manual_fix = { kind: "entered", value: "1002346" };
    const more = vm.runInContext("lookupDetails", context)(trace).children[1].children.map((el) => el.textContent);
    assert.deepEqual(more.slice(-4), ["Rows returned", "1, 2, 3, 4, 5 (5 of 9)", "Manual fix", "entered: 1002346"]);
    // lookup_trace.printed comes from the invoice: it is shown as text only (the doubles throw on innerHTML).
    const hostile = '<img src=x onerror=alert(1)>';
    const shown = vm.runInContext("lookupDetails", context)({ printed: { vpn: [hostile], barcode: [], description: hostile } });
    assert(shown.children[1].children.filter((el) => el.textContent === hostile).length === 2);
    // Line 3: no suggestion, so no Yes; Not in master points at the item master.
    assert(!button(missing, "Yes"));
    await button(missing, "Not in master").emit("click");
    assert(!find(missing, (el) => el.textContent === "Add it to the item master, then Update item master.").hidden);
    // The table no longer carries its own Item entry for prompted lines (one input per line).
    const tableEntries = walk(node("#rules-lines")).filter((el) => el.className === "rules-entry" && el.dataset.name === "Item");
    assert.equal(tableEntries.length, 0);
    // Update item master: POST (202 {id, reason, status}), poll GET until done, then show counts and line changes.
    const requests = [];
    const polls = [{ id: "r1", reason: "manual", status: "running" }, { id: "r1", reason: "manual", status: "done",
      counts: { rematched: 2, exported_skipped: 1, not_ready_skipped: 0, failed: 0 },
      invoices: [{ job_id: "job-1", filename: "a.pdf", number: "INV-1", lines: { newly_matched: [{ line: 3, item: "100000001" }],
        lost: [{ line: 4, item: "A" }], changed: [{ line: 5, from: "B", to: "B2" }] } }] }];
    context.fetch = async (path, init = {}) => {
      const method = init.method || "GET";
      requests.push([path, method]);
      const body = path !== "/api/item-master/refresh" ? job
        : method === "POST" ? { id: "r1", reason: "manual", status: "queued" } : polls.shift();
      return { ok: true, status: method === "POST" ? 202 : 200, headers: { get: () => "application/json" }, json: async () => body };
    };
    vm.runInContext("app.state = app.state || {}; app.state.jobs = app.state.jobs || []", context);
    await vm.runInContext("updateItemMaster()", context).catch(() => {});
    assert.deepEqual(requests.slice(0, 3), [["/api/item-master/refresh", "POST"], ["/api/item-master/refresh", "GET"],
      ["/api/item-master/refresh", "GET"]]);
    const refreshed = node("#refresh-result");
    assert(!refreshed.hidden);
    assert.equal(refreshed.children[0].textContent, "Item master updated: 2 re-matched · 1 exported skipped · 0 not ready · 0 failed.");
    assert.equal(refreshed.children[1].children[0].textContent, "INV-1: line 3 now 100000001; line 4 lost A; line 5 B → B2");
    // Full coverage: banner says ready, no prompts, no button.
    job.rules.coverage = { matched: 3, total: 3, pct: "100.00", tier: "Target", export: "yes", low: false, prompt: null, missing: [] };
    job.rules.fields = {};
    job.rules.lines = [{ line: 1, cells: { Item: ok("1") } }];
    vm.runInContext("renderRulesResult(job)", context);
    assert.equal(node("#coverage-text").textContent, "Coverage 3 of 3 lines (100%) · Target · ready to export");
    assert(node("#update-item-master").hidden && node("#rules-problems").hidden);
    // No coverage payload yet (older server): the old item rate line shows, no banner.
    delete job.rules.coverage;
    vm.runInContext("renderRulesResult(job)", context);
    assert(node("#coverage-banner").hidden && !node("#rules-item-rate").hidden);
    return;
  }
  if (scenario === "bundle") {
    // The built Entra bundle: the app.js seam is there, local mode needs no sign-in, and an incomplete or
    // multi-tenant configuration is refused before MSAL is created.
    let config = { cloud: false };
    context.fetch = async () => ({ ok: true, status: 200, json: async () => config });
    context.window = context;
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const auth = context.InvoiceStudioAuth;
    assert.deepEqual(Object.keys(auth).sort(), ["bootstrap", "currentUser", "getToken", "signIn", "signOut"]);
    assert.deepEqual({ ...(await auth.bootstrap()) }, { cloud: false, user: null });
    assert.equal(await auth.getToken(), null);
    const tenant = "11111111-2222-3333-4444-555555555555";
    const client = "66666666-7777-8888-9999-000000000000";
    for (const entra of [undefined, { clientId: client, tenantId: "common", authority: "https://login.microsoftonline.com/common", scopes: ["s"] },
      { clientId: client, tenantId: tenant, authority: "https://login.example.test/" + tenant, scopes: ["s"] },
      { clientId: "", tenantId: tenant, authority: "https://login.microsoftonline.com/" + tenant, scopes: ["s"] },
      { clientId: client, tenantId: tenant, authority: "https://login.microsoftonline.com/" + tenant, scopes: [] }]) {
      config = { cloud: true, entra };
      await assert.rejects(auth.bootstrap(), /not configured completely/);
    }
    return;
  }
  if (scenario === "sign-in" || scenario === "redirect-error" || scenario === "no-role") {
    // The gate offers only "Sign in with Microsoft" (plus retry / another account), never a password or invite path.
    const responses = { "/api/public-config": { status: 200, body: { cloud: true } } };
    responses["/api/session"] = scenario === "no-role"
      ? { status: 403, body: { detail: "This account has not been granted access to this workspace" } }
      : { status: 200, body: { uid: "oid-1", email: hostileEmail, name: "N", role: "member" } };
    const requests = [];
    context.fetch = async (path, options = {}) => {
      requests.push({ path, options });
      const { status, body } = responses[path] || { status: 200, body: { jobs: [] } };
      return { ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => body };
    };
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    vm.runInContext("applySidebarMode = () => {}; setupPaneResizers = () => {}; setupModelPicker = () => {};", context);
    window.matchMedia = () => ({ addEventListener() {} });
    context.clearInterval = () => {};
    vm.runInContext("bindEvents(); startWorkspace = async () => { app.started = true; }; clearSelectedJob = () => {};", context);
    if (scenario === "redirect-error") sdk.runtime.error = "This account has not been granted access to this workspace. Ask your administrator to add you to the Invoice Studio group.";
    if (scenario === "no-role") sdk.runtime.user = { email: "x@example.test", name: "" };
    await vm.runInContext("initializeAccess()", context);
    for (const gone of ["#google-sign-in", "#password-sign-in", "#password-reset", "#auth-send-verification", "#user-create-form", "#user-link-dialog"]) {
      assert(!nodes.has(gone), `${gone} is still used`);
    }
    if (scenario === "no-role") {
      assert(node("#microsoft-sign-in").hidden && !node("#auth-sign-out").hidden);
      assert(node("#auth-message").textContent === "This account does not have workspace access.");
      assert(node("#auth-detail").textContent.includes("add you to the Invoice Studio group"));
      const session = requests.find((request) => request.path === "/api/session");
      assert(session.options.headers.Authorization === "Bearer synthetic-token");
      assert(!vm.runInContext("app.started", context));
      return;
    }
    assert(!node("#microsoft-sign-in").hidden && node("#auth-retry").hidden);
    assert(node("#auth-message").textContent.includes("work account"));
    assert(node("#auth-detail").hidden === (scenario === "sign-in"));
    assert(node("#auth-sign-out").hidden === (scenario === "sign-in"));
    if (scenario === "redirect-error") assert(node("#auth-detail").textContent.includes("Invoice Studio group"));
    await node("#microsoft-sign-in").emit("click");
    assert(signIns.length === 1 && !node("#microsoft-sign-in").disabled);
    await node("#auth-sign-out").emit("click");
    assert(signOuts.length === 1 && !node("#microsoft-sign-in").hidden);
    assert(requests.every((request) => !request.path.includes("password")));
    return;
  }
  if (scenario === "users") {
    // Read-only: a row per person who signed in, no buttons, no role selector, nothing that changes access.
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    context.fixture = { oid: "oid-me", email: hostileEmail, name: '<svg onload="alert(1)">', role: "admin",
      first_login_at: "2026-10-01T09:00:00+00:00", last_login_at: "2026-10-29T09:00:00+00:00" };
    context.other = { oid: "oid-2", email: "m@example.test", name: "", role: "member",
      first_login_at: "2026-10-02T09:00:00+00:00", last_login_at: "2026-10-03T09:00:00+00:00" };
    vm.runInContext("app.sessionUser = {uid: 'oid-me', role: 'admin'}; app.users = [fixture, other]; app.userGroups = {admin: 'IS Admins', member: ''}; renderUsers();", context);
    const [mine, theirs] = node("#users-rows").children;
    assert(mine.children.length === 5 && theirs.children.length === 5);
    assert(mine.children[0].textContent === context.fixture.name + " (you)");
    assert(mine.children[1].textContent === hostileEmail && mine.children[2].textContent === "Administrator");
    assert(theirs.children[0].textContent === "—" && theirs.children[2].textContent === "Member");
    const controls = (element) => element.children.flatMap((child) => [child, ...controls(child)]);
    assert(!controls(node("#users-rows")).some((element) => ["BUTTON", "SELECT", "INPUT"].includes(element.tagName)));
    const intro = node("#users-intro").textContent;
    assert(intro.includes("“IS Admins”") && intro.includes("member group") && intro.includes("does not grant or remove access"));
    vm.runInContext("app.users = []; renderUsers();", context);
    assert(node("#users-rows").children[0].children[0].textContent === "Nobody has signed in yet.");
    return;
  }
  if (scenario === "batch-upload") {
    // The upload path of app.js against a scripted server: three uploads at a time, back-off on 429 / 5xx / a
    // dropped connection, a refused file fails at once, failures stay listed with a retry, plans are renewed.
    const intervals = [];
    const cleared = [];
    context.setInterval = (callback, delay) => { intervals.push({ callback, delay }); return intervals.length; };
    context.clearInterval = (id) => cleared.push(id);
    context.FormData = class { constructor() { this.parts = []; } append(...part) { this.parts.push(part); } };
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const notices = [];
    context.recordNotice = (text) => notices.push(text);
    vm.runInContext(`notify = (text) => recordNotice(text); loadState = async () => {}; revealNewestJobs = () => {};
      selectJob = async () => {}; renderJobs = () => {}; updateUploadReadiness = () => {}; app.state = { jobs: [] };`, context);
    const requests = [];
    const scripted = {
      "a.txt": [429, 429, 200], "b.txt": [400], "c.txt": ["network", 503, 200], "d.txt": [200],
      "e.txt": [429, 429, 429, 429, 429, 429, 200], "f.txt": [409],
    };
    const detail = { 400: "Invoice must be between 1 byte and 12 MB", 409: "Processing plan expired. Review it again.",
      429: "Processing queue is full (500 invoices waiting or processing). Wait for some to finish.", 503: "unavailable" };
    let inFlight = 0;
    let peak = 0;
    const reply = (status, body) => ({ ok: status < 400, status, headers: { get: (name) => (name === "content-type" ? "application/json" : null) }, json: async () => body });
    let queueReply = { queued: [], processing: [] };
    const jobReplies = {};
    context.fetch = async (path, options) => {
      requests.push({ path, options });
      if (path === "/api/queue") return reply(200, queueReply);
      if (path.startsWith("/api/jobs/")) return reply(200, jobReplies[decodeURIComponent(path.slice(10))]);
      if (path === "/api/preflight/renew") return reply(200, { renewed: true });
      assert(path === "/api/invoices" && options.headers["X-Studio-Request"] === "1");
      const name = options.body.parts.find((part) => part[0] === "file")[2];
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setImmediate(resolve));
      inFlight -= 1;
      const status = scripted[name].shift();
      if (status === "network") throw new TypeError("Failed to fetch");
      return reply(status, status === 200 ? { id: `job-${name[0]}`, status: "queued", filename: name } : { detail: detail[status] });
    };
    vm.runInContext("app.uploadFiles = ['a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt', 'f.txt'].map((name) => ({ name, size: 10 }))", context);
    await vm.runInContext("runUpload('plan-token', { engine: 'auto' })", context);
    const uploads = requests.filter((request) => request.path === "/api/invoices");
    const uploadsOf = (name) => requests.filter((request) => request.path === "/api/invoices"
      && request.options.body.parts.find((part) => part[0] === "file")[2] === name).length;
    assert.deepEqual(["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"].map(uploadsOf), [3, 1, 3, 1, 6, 1], "retry counts");
    assert(peak <= 3 && peak >= 2, `uploads in flight peaked at ${peak}`);
    assert(uploads.every((request) => request.options.body.parts.some((part) => part[0] === "preflight_token" && part[1] === "plan-token")));
    const backoffs = delays.filter((delay) => delay >= 1000);
    assert.deepEqual([...backoffs].sort((x, y) => x - y), [1000, 1000, 1000, 2000, 2000, 2000, 4000, 8000, 16000], `back-off delays ${backoffs}`);
    const batch = vm.runInContext("app.batch", context);
    const statusOf = (name) => batch.entries.find((entry) => entry.file.name === name).status;
    assert.deepEqual(["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"].map(statusOf), ["queued", "failed", "queued", "queued", "failed", "failed"]);
    assert(notices.at(-1).startsWith("3 files could not be added"));
    const panel = node("#upload-progress");
    assert(!panel.hidden && panel.children[0].textContent === "3 of 6 uploaded");
    assert.deepEqual(panel.children[1].children.map((chip) => chip.textContent), ["Queued 3", "Processing 0", "Done 0", "Failed 3"]);
    const failedList = panel.children[2];
    assert(failedList.children[0].textContent === "Review rules again for 1 file");
    const rows = failedList.children.slice(1);
    assert.deepEqual(rows.map((row) => row.children[0].textContent), [
      "b.txt: Invoice must be between 1 byte and 12 MB", `e.txt: ${detail[429]}`, "f.txt: Processing plan expired. Review it again."]);
    assert.deepEqual(rows.map((row) => row.children[1].textContent), ["Retry", "Retry", "Review rules"]);
    // Plan renewal ran on a timer while uploads were in flight, for the plan the pending files belong to.
    const renewal = intervals.find((interval) => interval.delay === 240000);
    assert(renewal && cleared.includes(intervals.indexOf(renewal) + 1), "renewal timer was not started and stopped");
    vm.runInContext("app.batch.entries[0].status = 'pending'", context);
    await renewal.callback();
    vm.runInContext("app.batch.entries[0].status = 'queued'", context);
    const renew = requests.filter((request) => request.path === "/api/preflight/renew");
    assert(renew.length === 1 && JSON.parse(renew[0].options.body).token === "plan-token");
    // One click retries the failed upload under the same plan; the file was never re-picked.
    await rows[1].children[1].listeners.click[0]();
    assert(uploadsOf("e.txt") === 7 && statusOf("e.txt") === "queued");
    assert(node("#upload-progress").children[0].textContent === "4 of 6 uploaded");
    // An expired plan sends the file back to the upload queue for a fresh plan instead of retrying blindly.
    const expiredRow = node("#upload-progress").children[2].children.slice(1).find((row) => row.children[0].textContent.startsWith("f.txt"));
    await expiredRow.children[1].listeners.click[0]();
    assert(vm.runInContext("app.uploadFiles.map((file) => file.name).join()", context) === "f.txt");
    assert(!vm.runInContext("app.batch.entries.some((entry) => entry.file.name === 'f.txt')", context));
    // The batch poll asks the queue once and fetches only the jobs that left it.
    queueReply = { queued: ["job-c", "job-e"], processing: ["job-a"] };
    jobReplies["job-d"] = { id: "job-d", status: "review" };
    requests.length = 0;
    await vm.runInContext("pollBatch(app.batch)", context);
    assert.deepEqual(requests.map((request) => request.path), ["/api/queue", "/api/jobs/job-d"]);
    assert.deepEqual(node("#upload-progress").children[1].children.map((chip) => chip.textContent), ["Queued 2", "Processing 1", "Done 1", "Failed 1"]);
    queueReply = { queued: [], processing: [] };
    jobReplies["job-a"] = { id: "job-a", status: "review" };
    jobReplies["job-c"] = { id: "job-c", status: "error", error: "Processing failed (ValueError)." };
    jobReplies["job-e"] = { id: "job-e", status: "review" };
    vm.runInContext("startBatchPolling(app.batch)", context);
    const poll = intervals.find((interval) => interval.delay === 1500);
    assert(poll, "batch polling was not started");
    await vm.runInContext("pollBatch(app.batch)", context);
    assert.deepEqual(node("#upload-progress").children[1].children.map((chip) => chip.textContent), ["Queued 0", "Processing 0", "Done 3", "Failed 2"]);
    const processingFailure = node("#upload-progress").children[2].children.find((row) => row.children[0].textContent.startsWith("c.txt"));
    assert(processingFailure.children.length === 1, "a job that failed in processing is not offered an upload retry");
    assert(cleared.includes(intervals.indexOf(poll) + 1), "polling kept running after the batch settled");
    assert(!vm.runInContext("batchActive(app.batch)", context));
    return;
  }
  throw Error(`unknown scenario ${scenario}`);
}

main().catch((error) => { process.stderr.write(error.message + "\n"); process.exitCode = 1; });
