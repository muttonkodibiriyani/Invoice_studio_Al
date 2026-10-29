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
  const storageTouches = [];
  const forbiddenStorage = new Proxy({}, {
    get() { storageTouches.push("get"); throw Error("storage touched"); },
    set() { storageTouches.push("set"); throw Error("storage touched"); },
  });
  const location = { pathname: "/set-password", hash: "#oobCode=synthetic-action", search: "" };
  const replacements = [];
  const confirmations = [];
  const delays = [];
  const injectedScripts = [];
  let verifications = 0;
  let recovered = false;
  const terminalVerify = { expired: "expired-action-code", invalid: "invalid-action-code", disabled: "user-disabled" };
  const hostileEmail = '<img src=x onerror="alert(1)">@example.test';
  const window = {
    localStorage: forbiddenStorage, sessionStorage: forbiddenStorage,
    InvoiceStudioAuth: {
      async verifyResetCode(code) {
        verifications++;
        assert(code === "synthetic-action");
        assert(location.hash === "" && location.search === "", "URL was not cleared before verification");
        if (terminalVerify[scenario] || scenario === "network") {
          throw Object.assign(Error("provider internals must stay private"), {
            code: `auth/${terminalVerify[scenario] || "network-request-failed"}`,
          });
        }
        if ((scenario === "verify-transient-success" && verifications < 3)
          || (scenario === "verify-transient-exhaust" && !recovered)) {
          // Public-config failures have no Firebase auth/ error code.
          throw Error("public-config unavailable");
        }
        if (scenario === "verify-other-auth-error" && verifications < 3) {
          throw Object.assign(Error("temporary provider failure"), { code: "auth/internal-error" });
        }
        return hostileEmail;
      },
      async confirmReset(code, password) {
        assert(code === "synthetic-action");
        confirmations.push(password);
        if (["confirm-expired", "confirm-disabled", "weak"].includes(scenario)) {
          throw Object.assign(Error("provider internals must stay private"), {
            code: `auth/${scenario === "weak" ? "weak-password" : scenario === "confirm-disabled" ? "user-disabled" : "expired-action-code"}`,
          });
        }
        if ((scenario === "confirm-transient-success" && confirmations.length < 3)
          || (scenario === "confirm-transient-exhaust" && !recovered)) {
          throw Object.assign(Error("temporary network failure"), { code: "auth/network-request-failed" });
        }
      },
    },
  };
  const sdk = window.InvoiceStudioAuth;
  document.head = {
    append(script) {
      assert(script.tagName === "SCRIPT" && script.src === "/static/firebase-auth.bundle.js");
      assert(script.textContent === "", "module recovery must not inject inline code");
      assert(location.hash === "" && location.search === "");
      injectedScripts.push(script);
      queueMicrotask(() => {
        if (scenario === "module-fails" && !recovered) script.emit("error");
        else { window.InvoiceStudioAuth = sdk; script.emit("load"); }
      });
    },
  };
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
    assert(box.hidden && !box.children.length, "a job with no readers record shows nothing");
    // The running view lists the stages in the order the server reads them: PDF text before a chosen OCR reader.
    const stages = (job) => vm.runInContext(`plannedProcessingStages(${JSON.stringify(job)})`, context);
    assert.deepEqual([...stages({ filename: "a.pdf", options: { engine: "paddleocr" }, progress: { engine: "native_pdf_text" } })], ["native_pdf_text", "paddleocr"]);
    assert.deepEqual([...stages({ filename: "a.png", options: { engine: "docling" } })], ["docling"]);
    assert.deepEqual([...stages({ filename: "a.pdf", options: { engine: "auto", ai_fallback: true, provider: "openai" } })],
      ["invoice2data", "paddleocr", "docling", "openai"]);
    return;
  }
  if (scenario === "users") {
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    // Layout setup is unrelated to user management; all real user-event bindings still run.
    vm.runInContext("applySidebarMode = () => {}; setupPaneResizers = () => {}; setupModelPicker = () => {};", context);
    window.matchMedia = () => ({ addEventListener() {} });
    vm.runInContext("bindEvents()", context);
    context.fixture = { uid: "synthetic-uid", email: hostileEmail, name: '<svg onload="alert(1)">', role: "member", status: "invited" };
    vm.runInContext("app.sessionUser = {uid: 'owner'}; app.users = [fixture]; renderUsers();", context);
    const row = node("#users-rows").children[0];
    assert(row.children[0].textContent === hostileEmail);
    assert(row.children[1].textContent === context.fixture.name);
    context.result = { user: context.fixture, kind: "invite", link: "synthetic-link-value" };
    vm.runInContext("showUserLink(result)", context);
    assert(node("#user-link-dialog").open);
    assert(node("#user-link-text").selected);
    assert(node("#user-link-text").value === context.result.link);
    assert(node("#user-link-note").textContent.includes(hostileEmail));
    let copied = "";
    const notifications = [];
    context.navigator = { clipboard: { async writeText(value) { copied = value; } } };
    context.recordNotice = (text) => notifications.push(text);
    vm.runInContext("notify = (text) => recordNotice(text)", context);
    await vm.runInContext("copyUserLink()", context);
    assert(copied === context.result.link);
    assert(notifications[0] === "Link copied.");
    context.navigator.clipboard.writeText = async () => { throw Error("clipboard blocked"); };
    await vm.runInContext("copyUserLink()", context);
    assert(notifications[1].includes("copy it manually"));
    await node("#user-link-dialog").close();
    assert(node("#user-link-text").value === "", "link retained after dialog close");
    // Links are offered only for sign-ins the app created; a shared sign-in gets a note instead of a button.
    const actionsOf = (tableRow) => tableRow.children[5].children[0].children;
    assert(actionsOf(row)[0].dataset.userAction === "password-link" && actionsOf(row)[0].textContent === "Create-password link");
    context.fixture.login_origin = "app";
    context.shared = { uid: "shared-uid", email: "shared@example.test", name: "", role: "admin", status: "active",
      login_origin: "existing", reset_requested_at: "2026-10-22T17:59:14+00:00" };
    vm.runInContext("app.users = [fixture, shared]; renderUsers();", context);
    const [appRow, sharedRow] = node("#users-rows").children;
    assert(actionsOf(appRow)[0].dataset.userAction === "password-link");
    assert(!actionsOf(sharedRow).some((element) => element.dataset.userAction === "password-link"), "link offered for a shared sign-in");
    assert(actionsOf(sharedRow)[0].textContent.includes("Shared sign-in"));
    assert(sharedRow.children[3].children.some((element) => element.textContent.startsWith("Reset requested")));
    // While the runtime may not manage sign-ins, the provider's refusal is explained instead of shown raw.
    vm.runInContext("api = async () => { throw new Error('The identity provider rejected the request: PermissionDeniedError'); }", context);
    const control = { tagName: "BUTTON", disabled: false, dataset: { userAction: "password-link" }, closest: () => ({ dataset: { uid: "synthetic-uid" } }) };
    await vm.runInContext("userAction", context)({ type: "click", target: { closest: () => control } });
    assert(notifications.at(-1).includes("not yet allowed") && notifications.at(-1).includes("Nothing was changed"));
    assert(!control.disabled && !node("#user-link-dialog").open);
    assert(vm.runInContext("userAdminError(new Error('Enter a valid email address'))", context) === "Enter a valid email address");
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
  if (scenario === "forgot") {
    vm.runInContext(fs.readFileSync(script, "utf8"), context);
    const requests = [];
    let status = 200;
    context.fetch = async (path, options) => { requests.push({ path, options }); return { ok: status < 400, status }; };
    node("#auth-email").value = " someone@example.test ";
    await vm.runInContext("resetPassword()", context);
    assert(requests.length === 1 && requests[0].path === "/api/password/forgot");
    assert(JSON.parse(requests[0].options.body).email === "someone@example.test");
    assert(!("Authorization" in requests[0].options.headers) && requests[0].options.headers["X-Studio-Request"] === "1");
    // The page must not promise an email: nothing is sent, the administrator hands the link over.
    assert(node("#auth-message").textContent.includes("No email is sent"));
    assert(node("#auth-detail").textContent.includes("administrator") && !node("#auth-detail").hidden);
    assert(!node("#password-reset").disabled);
    status = 429;
    await vm.runInContext("resetPassword()", context);
    assert(node("#auth-message").textContent === "Too many requests.");
    node("#auth-email").value = "";
    await vm.runInContext("resetPassword()", context);
    assert(requests.length === 2 && node("#auth-email").focused);
    return;
  }

  if (scenario === "missing" || scenario === "query-code") location.hash = "";
  if (scenario === "query-code") location.search = "?oobCode=synthetic-action";
  if (scenario === "fragment-and-query") location.search = "?irrelevant=value";
  if (scenario === "module-missing" || scenario === "module-fails") window.InvoiceStudioAuth = undefined;
  for (const selector of ["#sp-form", "#sp-sign-in", "#sp-detail", "#sp-retry"]) node(selector).hidden = true;
  vm.runInContext(fs.readFileSync(script, "utf8"), context);
  await document.DOMContentLoaded();
  const privateStateIsClean = () => {
    assert(storageTouches.length === 0, "password page accessed browser storage");
    assert(location.hash === "" && location.search === "");
    assert(!node("#sp-detail").textContent.includes("provider internals"));
    assert(!node("#sp-detail").textContent.toLowerCase().includes("reload"));
  };
  privateStateIsClean();
  if (scenario === "missing" || scenario === "query-code") {
    assert(verifications === 0);
    assert(node("#sp-form").hidden);
    assert(node("#sp-retry").hidden);
    assert(node("#sp-message").textContent.includes("incomplete"));
    assert(document.activeElement === node("#sp-message"));
    assert(replacements.length === (scenario === "query-code" ? 1 : 0));
    return;
  }
  assert(replacements.length === 1 && replacements[0] === "/set-password");
  if (terminalVerify[scenario]) {
    assert(verifications === 1 && delays.length === 0);
    assert(node("#sp-form").hidden);
    assert(node("#sp-sign-in").hidden);
    assert(node("#sp-retry").hidden);
    assert(document.activeElement === node("#sp-message"));
    assert(node("#sp-detail").textContent.includes(scenario === "disabled" ? "disabled" : "expired or was already used"));
    return;
  }
  if (["network", "verify-transient-exhaust", "module-fails"].includes(scenario)) {
    assert.deepEqual(delays, [2000, 4000, 8000, 15000]);
    assert(!node("#sp-retry").hidden && !node("#sp-retry").disabled);
    assert(node("#sp-form").hidden && node("#sp-sign-in").hidden);
    assert(document.activeElement === node("#sp-message"));
    if (scenario === "module-fails") assert(injectedScripts.length === 5 && verifications === 0);
    else assert(verifications === 5);
    if (scenario === "network") return;
    recovered = true;
    await node("#sp-retry").emit("click");
    privateStateIsClean();
    assert(node("#sp-retry").hidden && !node("#sp-form").hidden);
    if (scenario === "module-fails") assert(injectedScripts.length === 6 && verifications === 1);
    else assert(verifications === 6);
  } else if (["verify-transient-success", "verify-other-auth-error"].includes(scenario)) {
    assert(verifications === 3);
    assert.deepEqual(delays, [2000, 4000]);
  } else {
    assert(verifications === 1 && delays.length === 0);
  }
  if (scenario === "module-missing") assert(injectedScripts.length === 1);
  assert(injectedScripts.every((script) => script.removed));
  assert(node("#sp-email").value === hostileEmail);
  assert(!node("#sp-form").hidden);
  node("#sp-password").value = node("#sp-confirm").value = "short";
  await node("#sp-form").emit("submit");
  assert(confirmations.length === 0);
  assert(node("#sp-detail").textContent.includes("12 characters"));
  assert(document.activeElement === node("#sp-message"));
  node("#sp-password").value = "a sufficiently long password";
  node("#sp-confirm").value = "a different long password";
  await node("#sp-form").emit("submit");
  assert(confirmations.length === 0);
  assert(node("#sp-detail").textContent.includes("do not match"));
  node("#sp-confirm").value = node("#sp-password").value;
  delays.length = 0;
  if (scenario === "confirm-module-missing") window.InvoiceStudioAuth = undefined;
  await node("#sp-form").emit("submit");
  privateStateIsClean();
  if (["confirm-expired", "confirm-disabled", "weak"].includes(scenario)) {
    assert(confirmations.length === 1 && delays.length === 0);
    assert(node("#sp-form").hidden === (scenario !== "weak") && node("#sp-sign-in").hidden);
    assert(node("#sp-detail").textContent.includes(scenario === "weak" ? "stronger password" : scenario === "confirm-disabled" ? "disabled" : "expired or was already used"));
    assert(node("#sp-retry").hidden);
    assert(document.activeElement === node("#sp-message"));
    assert(!node("#sp-submit").disabled);
    return;
  }
  if (scenario === "confirm-transient-exhaust") {
    assert(confirmations.length === 5);
    assert.deepEqual(delays, [2000, 4000, 8000, 15000]);
    assert(!node("#sp-retry").hidden && !node("#sp-form").hidden);
    assert(document.activeElement === node("#sp-message"));
    recovered = true;
    await node("#sp-retry").emit("click");
    assert(confirmations.length === 6);
  } else if (scenario === "confirm-transient-success") {
    assert(confirmations.length === 3);
    assert.deepEqual(delays, [2000, 4000]);
  } else {
    assert(confirmations.length === 1 && delays.length === 0);
  }
  if (scenario === "confirm-module-missing") assert(injectedScripts.length === 1);
  assert(confirmations.every((password) => password === "a sufficiently long password"));
  assert(node("#sp-password").value === "" && node("#sp-confirm").value === "");
  assert(node("#sp-form").hidden && !node("#sp-sign-in").hidden);
  assert(node("#sp-retry").hidden);
  assert(!node("#sp-submit").disabled);
  assert(document.activeElement === node("#sp-sign-in"));
  privateStateIsClean();
}

main().catch((error) => { process.stderr.write(error.message + "\n"); process.exitCode = 1; });
