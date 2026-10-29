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
