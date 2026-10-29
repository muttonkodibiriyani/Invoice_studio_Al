"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

const flowDetails = {
  1: ["Input control", "Each selected file becomes one job; up to two jobs run at once and the rest stay queued.", "Supported document, image and structured-text formats are checked for type and size. One file must contain one invoice; combined invoices must be split before upload."],
  2: ["Decision control", "The reviewer sees the effective rules before any document is processed.", "The plan names the engine, optional provider and model, reference version, tolerances, files and warnings. Cancel sends no document and preserves the queue."],
  3: ["Extraction control", "The job names each stage; optional AI is a permitted fallback branch.", "A supplier template is tried first. A scanned invoice2data route names Paddle as its input. The trace separates failed engines from text-only attempts, and completeness is read coverage rather than accuracy."],
  4: ["Business control", "Canonical references approve; lookup only supplies source evidence.", "Atomic cutover completed with 412,139 item and PO/GRN evidence rows. Authenticated API ranking and strict-filter checks passed, but a person must still inspect and confirm every candidate; a lookup choice never makes an invoice ready."],
  5: ["Human control", "A person compares editable values with the source and confirms the evidence review.", "The original document, extracted evidence, derivations and validation issues stay together. Revision checks stop stale updates from overwriting newer work."],
  6: ["Release control", "Three workbook routes remain visibly separate.", "Combined EXTRACTION_REVIEW_ONLY exports saved processed facts without approval or allocation. Approved export revalidates and reserves receipts. The all-field DRAFT_UNVALIDATED path requires acknowledgement and reserves nothing."],
};

const stories = [
  {
    number: "INV-01", status: "working", persona: "AP operator", title: "Select and confirm an invoice batch",
    summary: "Know which files, data and reader will be used.", delivery: "Local and hosted processing gate verified",
    story: "As an AP operator, I want to choose several invoices and confirm the processing rules so I know which files, data and reader will be used.",
    acceptance: ["Cancel sends no invoice-processing request.", "A confirmed plan is bound to filenames/sizes, engine/model, reference version and tolerances.", "Changed rules require a fresh confirmation."],
  },
  {
    number: "INV-02", status: "working", persona: "AP operator", title: "Read digital invoices and scans",
    summary: "Avoid retyping supplier documents.", delivery: "Bounded local and hosted paths measured; broad 95% quality remains unproven",
    story: "As an AP operator, I want to extract headers and every item line from supported files so I avoid retyping supplier documents.",
    acceptance: ["Native PDF tables recovered 570/570 lines and 3,990/3,990 tested line facts from one layout family; a hosted native check recovered 193 rows and 1,351/1,351 line facts in 10.21 seconds.", "Paddle recovered 6/6 adjudicated scan lines with a conservative 60/66 = 90.91% floor. Its adaptive two-page path returned 96/99 (97.0%) and 23/23 rows locally, then the same result hosted on revision 00008 in 242.98 seconds.", "Docling recovered bounded native tables but only 10/23 lines on that challenging two-page scan with poor source-position matches; a ten-page timeout remains visible."],
  },
  {
    number: "INV-03", status: "pending", persona: "AP operator", title: "Use a chosen AI connection for exceptions",
    summary: "Let unfamiliar layouts be proposed for review.", delivery: "Hosted managed-Vertex checks passed; representative accuracy and live subscription inference remain pending",
    story: "As an AP operator, I want to select a provider/model and enable automatic fallback so unfamiliar layouts can be proposed for review.",
    acceptance: ["Managed Vertex uses the workspace identity/project billing; every AI call requires explicit selection or confirmed fallback.", "Saved credentials are distinct from verified provider access; model auto-offer does not replace a still-connected user selection.", "All source-tested fields were exact on 15 documents/570 lines from one layout family; that cohort does not prove the expected 95% across suppliers, scans, languages and lengths."],
  },
  {
    number: "INV-04", status: "planned", persona: "Data steward", title: "Map and approve business reference extracts",
    summary: "Make the validator use evidence with known meaning.", delivery: "412,139 evidence rows live; API lookup QA passed; approval mappings unresolved",
    story: "As a data steward, I want to profile the whole item and PO/GRN files and approve explicit mappings so the validator uses evidence with known meaning.",
    acceptance: ["Preserve original bytes, hashes and row/column provenance; inspect all populated cells.", "Product-name and identifier lookup shows conflicts plus price/quantity/unit clues and requires explicit human confirmation.", "Printed buyer name remains separate from the internal buyer/company code; lookup evidence never becomes approved matching data."],
  },
  {
    number: "INV-05", status: "working", persona: "AP operator", title: "Match the exact supplier route and items",
    summary: "Prevent approval from leaking across markets or entities.", delivery: "Canonical matching implemented and tested",
    story: "As an AP operator, I want to check supplier, site, company, PO, item identity and unit so an invoice cannot borrow approval from another market or entity.",
    acceptance: ["Domestic and cross-border routes require their exact approval tuple.", "Conflicting SKU and barcode identity is held.", "Location type comes from an explicit reference field, not a numeric prefix."],
  },
  {
    number: "INV-06", status: "working", persona: "Finance owner", title: "Protect receipt quantity and commercial controls",
    summary: "Avoid incorrect or duplicate charges.", delivery: "Implemented and tested",
    story: "As a finance owner, I want to check price, accepted receipt balance and tax with decimal rules so we avoid incorrect or duplicate charges.",
    acceptance: ["Accepted receipts minus returns, external baseline and this app allocations determine available quantity.", "Repeated lines and multiple invoices consume quantity cumulatively.", "Tolerance and currency rounding are explicit; no credit or mixed-tax allocation is invented."],
  },
  {
    number: "INV-07", status: "working", persona: "AP reviewer", title: "Review evidence and resolve exceptions",
    summary: "Ensure every exported value has checked evidence.", delivery: "Implemented and browser verified",
    story: "As an AP reviewer, I want to compare the source with editable fields and record review so every exported value has checked evidence.",
    acceptance: ["Review confirmation cannot override validation holds.", "Printed line net/tax remain separate from unit price, and printed buyer name remains separate from the internal buyer/company code.", "Concurrent edits and batch selection require an explicit save; stale revisions are rejected."],
  },
  {
    number: "INV-08", status: "working", persona: "AP operator", title: "Download combined review or approved Excel",
    summary: "Keep unvalidated extraction facts separate from approved accounting output.", delivery: "Both routes implemented and browser/workbook verified locally",
    story: "As an AP operator, I want one combined workbook for saved processed facts and a separate approved export so review can proceed without implying business approval.",
    acceptance: ["Both routes use exact Header/Tax_Breakdown/Details sheets with 13/3/6 columns and transaction IDs 1..N.", "EXTRACTION_REVIEW_ONLY contains only saved processed jobs, records an audit event and changes no job, ledger or allocation.", "Approved export rechecks readiness and atomically reserves quantities; repeat download does not duplicate an allocation."],
  },
  {
    number: "INV-09", status: "working", persona: "Service owner", title: "Recover and audit local operations",
    summary: "Explain and retry a failed reader or restart.", delivery: "Instance-replacement persistence verified; database backup restoration remains a production gate",
    story: "As a service owner, I want to retain sources, rule versions, edits and exports so a failed reader or restart can be explained and retried.",
    acceptance: ["Restarted in-flight jobs visibly require retry; completed exports remain downloadable.", "Audit distinguishes text read, fields extracted and extraction failed; legacy extracted means only reader run finished.", "Recovery procedure preserves the database plus the corresponding encryption key."],
  },
  {
    number: "INV-10", status: "working", persona: "Workspace owner", title: "Run a restricted cloud workspace",
    summary: "Use a hosted portal without losing evidence on restart.", delivery: "Final revision 00009-vxw live; reader, lookup, control and owner-only allowlist checks passed",
    story: "As a workspace owner, I want to sign in to a Firebase frontend backed by durable GCP services so the team can use a hosted portal without losing evidence on restart.",
    acceptance: ["Unauthenticated and non-allowlisted accounts cannot access invoices, references or connections.", "Database and document evidence survive a replacement application instance.", "Final revision 00009-vxw uses the tested core image plus cancel/footer fixes; hosted reader, workbook, lookup, deletion/model and cancel-dialog checks passed; the owner-only allowlist, email-verified owner flag, unauthenticated denial and smoke-identity deletion checks passed."],
  },
  {
    number: "INV-11", status: "planned", persona: "Procurement owner", title: "Qualify suppliers for a new store or market",
    summary: "Avoid repeating every supplier task for store openings.", delivery: "Proposed next product increment",
    story: "As a procurement owner, I want to reuse approved coverage and start discovery only for gaps so store openings do not repeat every supplier task.",
    acceptance: ["Same-market coverage reuses valid route facts with provenance/expiry.", "New market or buying entity creates an explicit qualification case.", "AI suggestions cannot approve a legal entity, route or commercial agreement."],
  },
  {
    number: "INV-12", status: "planned", persona: "Item-data steward", title: "Normalize supplier items for buying and digital channels",
    summary: "Let purchasing and commerce reuse a consistent catalog.", delivery: "Proposed next product increment",
    story: "As an item-data steward, I want to turn varied supplier files into approved item identities and channel content so purchasing and commerce teams can reuse a consistent catalog.",
    acceptance: ["Supplier codes, internal items, barcodes, packaging and units have explicit mappings.", "Conflicting identities and unit conversions need steward approval.", "Channel-specific text, attributes, images and translations are versioned separately from purchasing identity."],
  },
  {
    number: "INV-13", status: "pending", persona: "AP operator", title: "Download an explicitly unvalidated manual draft",
    summary: "Keep urgent manual work moving during OCR.", delivery: "Implemented and hosted; job and ledger non-mutation passed",
    story: "As an AP operator, I want to edit every target workbook field and download a draft while OCR is still running.",
    acceptance: ["Header/Tax_Breakdown/Details expose exactly 13/3/6 editable fields with one joined transaction.", "Explicit acknowledgement and workbook disclosures say DRAFT_UNVALIDATED and no receipt reservation.", "Draft creation does not change the job, approve references, write the approved ledger or reserve receipts."],
  },
  {
    number: "INV-14", status: "pending", persona: "Data steward", title: "Search large source extracts without implying approval",
    summary: "Find source evidence without promoting it into matching.", delivery: "412,139 evidence rows live; authenticated API and UI QA passed",
    story: "As a data steward, I want to search actual item and PO sources by identifier or product name.",
    acceptance: ["Results retain source row, conflict context and visible name/price/quantity/unit clues with bounded pagination and filters.", "The operator explicitly confirms a selection before it populates the manual workspace.", "Lookup is never represented as matching approval, receipt allocation or export readiness."],
  },
  {
    number: "INV-15", status: "pending", persona: "Workspace owner", title: "Move an owner subscription connection",
    summary: "Use an authorized account without exposing it through browser state.", delivery: "Hosted model controls passed; live Claude/ChatGPT subscription inference remains unverified",
    story: "As a workspace owner, I want to connect restricted Claude or move my ChatGPT registration into my hosted workspace.",
    acceptance: ["Claude setup tokens are encrypted, removable and supplied only to the restricted CLI.", "ChatGPT export deletes local encrypted credentials, disconnects and clears the active account.", "Only authenticated cloud owners import; tests do not imply live eligibility or quality."],
  },
  {
    number: "INV-16", status: "pending", persona: "Workspace owner", title: "Permanently delete invoice data",
    summary: "Remove live evidence without breaking duplicate or receipt controls.", delivery: "Hosted deletion verification passed",
    story: "As a workspace owner, I want confirmed deletion with a clear retention receipt.",
    acceptance: ["Confirmation and current revisions are required; active jobs and partial exported batches are rejected atomically.", "Live jobs, source objects, detailed audit and workbook copies are removed.", "Only a minimal deletion audit and approved invoice-key/PO-line allocation tombstone remain; provider backups and versions follow provider retention."],
  },
];

let storyFilter = "all";

function setView(name, updateHash = true) {
  const valid = ["overview", "stories", "architecture", "roadmap", "decisions"];
  const selected = valid.includes(name) ? name : "overview";
  $$(".view").forEach((section) => {
    const active = section.dataset.section === selected;
    section.hidden = !active;
    section.classList.toggle("active", active);
  });
  $$(".section-nav [data-view]").forEach((button) => {
    const active = button.dataset.view === selected;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (updateHash) history.replaceState(null, "", `#${selected}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
  $("#content").focus({ preventScroll: true });
}

function setFlowStep(number) {
  $$(".flow-step").forEach((button) => button.classList.toggle("active", button.dataset.step === number));
  const [label, title, description] = flowDetails[number];
  const detail = $("#flow-detail");
  detail.replaceChildren(node("span", "", label), node("strong", "", title), node("p", "", description));
}

function storyStatus(status) {
  if (status === "working") return ["working", "Working MVP"];
  if (status === "pending") return ["pending", "Pending validation"];
  if (status === "untested") return ["untested", "Live AI untested"];
  return ["planned", "Planned"];
}

function renderStories() {
  const query = $("#story-search").value.trim().toLowerCase();
  const visible = stories.filter((story) => {
    const matchesFilter = storyFilter === "all" || story.status === storyFilter;
    const haystack = [story.number, story.persona, story.title, story.summary, story.story, ...story.acceptance].join(" ").toLowerCase();
    return matchesFilter && haystack.includes(query);
  });
  const list = $("#story-list");
  list.replaceChildren();
  visible.forEach((story) => {
    const card = node("details", "story-card");
    const summary = node("summary");
    const number = node("span", "story-number", story.number);
    const title = node("span", "story-title");
    title.append(node("strong", "", story.title), node("small", "", `${story.persona} · ${story.summary}`));
    const chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chevron.classList.add("chevron");
    chevron.setAttribute("viewBox", "0 0 20 20");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m6 8 4 4 4-4");
    chevron.append(path);
    summary.append(number, title, chevron);
    const body = node("div", "story-body");
    body.append(node("p", "", story.story));
    const acceptance = node("ol", "acceptance");
    story.acceptance.forEach((criterion) => acceptance.append(node("li", "", criterion)));
    body.append(acceptance);
    const meta = node("div", "story-meta");
    const [statusClass, statusLabel] = storyStatus(story.status);
    meta.append(node("span", `status ${statusClass}`, statusLabel), node("span", "", `${story.delivery} · ${story.acceptance.length} acceptance checks`));
    body.append(meta);
    card.append(summary, body);
    list.append(card);
  });
  $("#story-empty").hidden = visible.length > 0;
}

function setArchitecturePath(path) {
  $$('[data-path]').forEach((button) => button.classList.toggle("active", button.dataset.path === path));
  const local = $(".local-node");
  const ai = $(".ai-node");
  const caption = $("#architecture-caption");
  if (path === "ai") {
    ai.classList.add("highlight");
    ai.classList.remove("dim");
    local.classList.remove("highlight");
    caption.replaceChildren(node("strong", "", "AI fallback path selected: "), document.createTextNode("after a confirmed plan permits sharing, managed Vertex or another chosen provider can propose incomplete fields; its result still passes schema, reference validation and evidence review."));
  } else {
    local.classList.add("highlight");
    ai.classList.add("dim");
    ai.classList.remove("highlight");
    caption.replaceChildren(node("strong", "", "Local path selected: "), document.createTextNode("a matching template is tried first; otherwise conservative native rules require explicit invoice evidence and skip image OCR, while scans continue through local readers. Every stage remains visible."));
  }
}

function bind() {
  $$('[data-view]').forEach((control) => control.addEventListener("click", () => setView(control.dataset.view)));
  $$('[data-view-link]').forEach((control) => control.addEventListener("click", (event) => { event.preventDefault(); setView(control.dataset.viewLink); }));
  $$(".flow-step").forEach((button) => button.addEventListener("click", () => setFlowStep(button.dataset.step)));
  $$('[data-story-filter]').forEach((button) => button.addEventListener("click", () => {
    storyFilter = button.dataset.storyFilter;
    $$('[data-story-filter]').forEach((candidate) => candidate.classList.toggle("active", candidate === button));
    renderStories();
  }));
  $("#story-search").addEventListener("input", renderStories);
  $$('[data-path]').forEach((button) => button.addEventListener("click", () => setArchitecturePath(button.dataset.path)));
  $("#print-button").addEventListener("click", () => window.print());
  window.addEventListener("hashchange", () => setView(location.hash.slice(1), false));
}

bind();
renderStories();
setArchitecturePath("local");
setView(location.hash.slice(1) || "overview", false);
