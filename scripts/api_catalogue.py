"""What each Invoice Studio API route does, in one place: the source of docs/API.md, the Postman collection and the
PDF (scripts/build_api_docs.py). tests/test_api_docs.py fails when a route of the app is missing here or a route
here no longer exists, so the documentation cannot drift from the code.

Each entry: (METHOD, path) -> area, title, description, and optionally an example body (JSON or multipart form),
query parameters, and the status a smoke run expects.
"""

AREAS = (
    ("health", "Health and sign-in", "Public health check, sign-in settings and the signed-in session."),
    ("upload", "Processing plan and upload", "Every upload runs under a processing plan the same person previewed "
     "and confirmed: preflight, confirm, then upload each file named in the plan."),
    ("invoices", "Invoices", "Read one invoice, its source document, audit trail and target check."),
    ("edits", "Review and export", "Review edits, retry, approve and export one invoice."),
    ("exports", "Exports and downloads", "Download exports and batch workbooks; long downloads run in the background."),
    ("rules", "Fine rules and target export", "Mapping-table driven checks, review and target workbooks."),
    ("lookup", "Product and PO lookup", "Search the imported item master and PO/GRN records."),
    ("learned", "Learned layouts", "Supplier layouts learned from reviewed invoices."),
    ("samples", "Sample files", "Synthetic sample files for training and tests."),
    ("master", "Item master and mapping tables", "Admin uploads of the owner's master data, with validation, "
     "activation and rollback."),
    ("setup", "Reference data and policy", "Admin setup: references, supplier templates, tolerances."),
    ("ai", "AI connection and settings", "Admin control of AI. The Azure deployment allows Azure AI Foundry only "
     "(INV_STUDIO_AI_PROVIDERS=azure); other providers answer 403."),
    ("admin", "Administration", "Delete invoices, costs, users."),
)

LOCAL_ONLY = {"engine": "auto", "prefer_native_text": True, "ai_fallback": False, "ai_cross_check": False,
              "provider": "azure", "model": "", "language": "en"}

ROUTES = {
    # health
    ("GET", "/health"): dict(area="health", level="none", title="Health check",
        text="Answers {\"status\":\"ok\",\"version\":...} once the app is up. No token. Used by the platform probe.",
        smoke=200),
    ("GET", "/api/public-config"): dict(area="health", title="Sign-in settings",
        text="Tenant id, client id, scope and authority the browser needs to sign in. Public by design; holds no "
             "secret and no data.", smoke=200),
    ("GET", "/api/session"): dict(area="health", title="Who am I",
        text="The signed-in user as the API sees them: id, name, email and role (admin or member), read from the "
             "token on this request.", smoke=200),
    ("GET", "/api/state"): dict(area="health", title="Workspace state",
        text="Everything the main screen shows: settings, connections (never keys), references summary, recent "
             "invoices and exports.", smoke=200),
    ("GET", "/api/queue"): dict(area="health", title="Processing queue",
        text="Invoices queued and being read on this instance, and how many it admits.", smoke=200),
    ("GET", "/api/health/dependencies"): dict(area="health", title="Readiness of database and storage",
        text="Reads the database and lists the evidence container once each, through the private endpoints when "
             "they are locked down. 200 with ok for each, or 503 with the failed one; never an address or message.",
        smoke=200),
    # upload
    ("POST", "/api/preflight"): dict(area="upload", title="Preview a processing plan",
        text="Send the options and the name and size of every file. Returns a plan token, a summary and warnings. "
             "Up to 500 files of up to 12 MB each.",
        body={"options": LOCAL_ONLY, "files": [{"name": "invoice.pdf", "size": 2140}]}),
    ("POST", "/api/preflight/confirm"): dict(area="upload", title="Confirm the plan",
        text="Confirms the previewed plan. Only the account that made the plan can confirm it.",
        body={"token": "{{planToken}}"}),
    ("POST", "/api/preflight/renew"): dict(area="upload", title="Keep a plan alive",
        text="Extends a confirmed plan by 10 minutes while a long batch is still uploading.",
        body={"token": "{{planToken}}"}),
    ("POST", "/api/invoices"): dict(area="upload", title="Upload one invoice",
        text="Multipart form: file, options (the same JSON as the plan) and preflight_token. The file must be "
             "named in the confirmed plan with the same size. PDF, PNG, JPEG, TIFF, DOCX, XLSX, CSV or text; 12 MB "
             "at most. Returns the invoice record; reading continues in the background.",
        form={"file": "@samples/invoice.pdf", "options": LOCAL_ONLY, "preflight_token": "{{planToken}}"}),
    # invoices
    ("GET", "/api/jobs/{jid}"): dict(area="invoices", title="One invoice",
        text="Status (queued, processing, needs_review, ready, exported, error), extracted fields, lines, checks "
             "and the reader trace."),
    ("GET", "/api/jobs/{jid}/document"): dict(area="invoices", title="The uploaded document",
        text="The original file, served with its own content type, nosniff and a sandboxing CSP."),
    ("GET", "/api/jobs/{jid}/audit"): dict(area="invoices", title="Audit trail of one invoice",
        text="Every step recorded for this invoice: upload hash, reads, edits, approvals, exports."),
    ("GET", "/api/jobs/{jid}/target-check"): dict(area="invoices", title="Target check",
        text="Read-only: the invoice's target-sheet cells by status, with evidence and row checks."),
    ("GET", "/api/target-check/accuracy"): dict(area="invoices", title="Target-check accuracy",
        text="Confirm-time accuracy against owner truth, per field. Optional ?supplier=.", query={"supplier": ""}),
    # edits
    ("POST", "/api/jobs/{jid}/review"): dict(area="edits", title="Save review edits",
        text="Saves edited fields and lines for the given revision; confirm:true approves. A stale revision is "
             "refused with 409; a control character or another a workbook cannot hold with 400 (a value read from a "
             "document is cleaned instead).",
        body={"invoice": {"number": "INV-1"}, "revision": 1, "confirm": False}),
    ("POST", "/api/jobs/{jid}/retry"): dict(area="edits", title="Read again",
        text="Reads the invoice again under a new confirmed plan.",
        body={"options": LOCAL_ONLY, "preflight_token": "{{planToken}}"}),
    ("POST", "/api/jobs/{jid}/export"): dict(area="edits", title="Export one invoice",
        text="Creates the Excel export for an invoice at the given revision: the three template sheets with what was "
             "found, blanks blank. Open validation and review holds do not block it (owner decision D11); the receipt "
             "records them as open_holds. A duplicate is the same number from the same supplier site; with a site or "
             "number blank nothing can be called one, so the invoice exports. It gets the hold Possible Duplicate, "
             "which a review confirm does not clear, when the same file or (a site missing on either side) the same "
             "number was already exported. A re-scan with no number cannot be noticed. 409 only if it is still processing, not read, changed or a duplicate; an "
             "invoice already exported returns its existing export.", body={"revision": 1}),
    ("POST", "/api/jobs/{jid}/extraction-draft"): dict(area="edits", title="Unvalidated extraction workbook",
        text="Downloads what was read, before validation, as Excel. Needs acknowledge_unvalidated.",
        body={"revision": 1, "acknowledge_unvalidated": True}),
    ("POST", "/api/jobs/{jid}/draft"): dict(area="edits", title="Manual draft workbook",
        text="Builds a target workbook from hand-entered Header, Tax_Breakdown and Details. Never approves.",
        body={"acknowledge_unvalidated": True, "Header": {}, "Tax_Breakdown": {}, "Details": []}),
    # exports
    ("GET", "/api/exports/{eid}"): dict(area="exports", title="Download an export", text="The export workbook."),
    ("GET", "/api/exports/{eid}/evidence"): dict(area="exports", title="Evidence of an export",
        text="The evidence record behind an export: sources, hashes, approvals."),
    ("POST", "/api/exports/batch"): dict(area="exports", title="Export a batch",
        text="One workbook for several invoices; each item names the invoice and its revision. Holds do not block "
             "(owner decision D11): each invoice is written with what was found, blanks blank, and its receipt records "
             "the open holds for the export team to validate by hand. All or nothing on integrity: an invoice still "
             "processing or not read, changed, already exported or duplicated refuses the batch with 409. The workbook "
             "holds only the Header, Tax_Breakdown and Details sheets, Transaction Number 1..n (no Checks sheet).",
        body={"jobs": [{"id": "{{jobId}}", "revision": 1}]}),
    ("POST", "/api/exports/batch/check"): dict(area="exports", title="Check a batch before export",
        text="Read-only. Splits the selected invoices into ready (in transaction order, each with attention: the "
             "holds still open on it, information only since D11) and blocked, each blocked one with its reasons as "
             "code and message (Processing, Not Read, Changed, Exported, Duplicate). Possible Duplicate is a hold, so it "
             "shows under attention, not blocked; see Export one invoice. Send the ready ones to /api/exports/batch.", body={"jobs": [{"id": "{{jobId}}", "revision": 1}]}),
    ("GET", "/api/batches/{bid}"): dict(area="exports", title="Download a batch export", text="The batch workbook."),
    ("POST", "/api/exports/extraction-batch"): dict(area="exports", title="Unvalidated batch workbook",
        text="What was read for several invoices, before validation.",
        body={"jobs": [{"id": "{{jobId}}", "revision": 1}], "acknowledge_unvalidated": True}),
    ("POST", "/api/exports/target-draft"): dict(area="exports", title="Target draft for unapproved invoices",
        text="The draft of everything: the target workbook for all selected invoices, held ones included, with the "
             "Checks sheet. Marked DRAFT in the file name and on every sheet (red tab and header row, a note on A1, the "
             "printed header); sheet names and headers stay the template's. Never an export.",
        body={"jobs": [{"id": "{{jobId}}", "revision": 1}]}),
    ("POST", "/api/background/{kind}"): dict(area="exports", title="Start a long download",
        text="kind is target-draft, extraction-batch, export-check, export-batch, fine-rules-target or fine-rules-review; the "
             "body is that download's body. Returns a task id to poll.", body={"jobs": [{"id": "{{jobId}}", "revision": 1}]}),
    ("GET", "/api/background/tasks/{tid}"): dict(area="exports", title="Poll a long download",
        text="Running, or the finished file. Only the account that started it can read it."),
    # rules
    ("GET", "/api/fine-rules/config"): dict(area="rules", title="Fine-rules configuration", text="The active "
        "mapping-table configuration.", smoke=200),
    ("POST", "/api/fine-rules/config"): dict(area="rules", title="Save fine-rules configuration",
        text="Replaces the configuration (JSON object). Audited by field names and sizes, never values.",
        body={}),
    ("POST", "/api/fine-rules/run"): dict(area="rules", title="Run fine rules", text="Runs the rules over invoices.",
        body={"job_ids": ["{{jobId}}"]}),
    ("POST", "/api/fine-rules/review.xlsx"): dict(area="rules", title="Fine-rules review workbook",
        text="The rule findings as Excel.", body={"job_ids": ["{{jobId}}"]}),
    ("POST", "/api/fine-rules/target.xlsx"): dict(area="rules", title="Fine-rules target workbook",
        text="The target workbook after the rules.", body={"job_ids": ["{{jobId}}"]}),
    ("GET", "/api/fine-rules/feedback"): dict(area="rules", title="Rule feedback", text="Corrections sent by "
        "reviewers.", smoke=200),
    ("POST", "/api/fine-rules/feedback"): dict(area="rules", title="Send rule feedback",
        text="A reviewer's correction to a rule outcome, with evidence.",
        body={"invoice_line": "1", "original": "", "correction": "x", "reason": "", "evidence": "page 1"}),
    ("POST", "/api/fine-rules/feedback/{fid}/decision"): dict(area="rules", title="Decide on feedback",
        text="Admin accepts or rejects a correction.", body={"decision": "accepted", "approver": "", "version": ""}),
    ("GET", "/api/rules-assistant"): dict(area="rules", title="Rules assistant state",
        text="Whether the AI rules assistant is on and, if not, why.", smoke=200),
    ("POST", "/api/rules-assistant/test"): dict(area="rules", title="Test the rules assistant connection",
        text="Admin check of the Azure AI Foundry connection; the assistant stays off until it passes."),
    ("POST", "/api/rules-assistant/search"): dict(area="rules", title="Rules assistant: search the item master",
        text="Ranked item master rows for a code or description. No AI call.", body={"query": "rose cream"}),
    ("POST", "/api/rules-assistant/explain"): dict(area="rules", title="Rules assistant: explain a match",
        text="Why an invoice line matched an item master row or not, from the engine's facts.",
        body={"line": {"description": "Rose hand cream 50ml", "gtin": "", "sku": ""}}),
    ("POST", "/api/rules-assistant/propose"): dict(area="rules", title="Rules assistant: propose rules",
        text="Drafts rule rows for a line and logs them as Proposed rule feedback; an admin decides.",
        body={"line": {"description": "Rose hand cream 50ml", "gtin": "", "sku": ""}, "note": ""}),
    ("GET", "/api/target-export/config"): dict(area="rules", title="Target export settings",
        text="Which UPC column the target export fills.", smoke=200),
    ("POST", "/api/target-export/config"): dict(area="rules", title="Save target export settings",
        text="Admin sets the UPC column.", body={"upc": "empty"}),
    # lookup
    ("GET", "/api/reference-lookup/summary"): dict(area="lookup", title="Lookup summary",
        text="Counts of the imported item master and PO/GRN records.", smoke=200),
    ("GET", "/api/reference-lookup/search"): dict(area="lookup", title="Search items or PO/GRN",
        text="kind=item or po, q of 2 to 100 characters; paged with cursor and limit (1-50).",
        query={"kind": "item", "q": "widget", "limit": "25"}),
    ("GET", "/api/reference-lookup/products"): dict(area="lookup", title="Product candidates",
        text="Ranked item-master candidates for an invoice line.", query={"q": "widget", "limit": "10"}),
    # learned
    ("GET", "/api/learned"): dict(area="learned", title="Learned layouts", text="Layouts learned so far.", smoke=200),
    ("POST", "/api/learned/jobs/{jid}"): dict(area="learned", title="Learn from an invoice",
        text="Learns the supplier layout from a reviewed invoice."),
    ("DELETE", "/api/learned/{key}"): dict(area="learned", title="Forget a layout", text="Admin removes a layout."),
    # samples
    ("GET", "/api/samples/reference.xlsx"): dict(area="samples", title="Sample reference workbook",
        text="Synthetic reference data.", smoke=200),
    ("GET", "/api/samples/invoice.pdf"): dict(area="samples", title="Sample invoice", text="A synthetic invoice.",
        smoke=200),
    ("GET", "/api/samples/template.yml"): dict(area="samples", title="Sample supplier template",
        text="An invoice2data template.", smoke=200),
    # master
    ("GET", "/api/item-master"): dict(area="master", title="Item master versions",
        text="Uploaded and active item-master versions.", smoke=200),
    ("POST", "/api/item-master/validate"): dict(area="master", title="Validate an item master",
        text="Multipart file upload; validates without activating.", form={"file": "@item-master.xlsx"}),
    ("POST", "/api/item-master/uploads/{version}/activate"): dict(area="master", title="Activate an item master",
        text="Makes a validated upload the active version."),
    ("POST", "/api/item-master/versions/{version}/rollback"): dict(area="master", title="Roll back the item master",
        text="Returns to an earlier version; ?expected_signature= guards against a concurrent change.",
        query={"expected_signature": ""}),
    ("DELETE", "/api/item-master/uploads/{version}"): dict(area="master", title="Discard an upload",
        text="Removes a validated upload that was never activated."),
    ("POST", "/api/item-master/refresh"): dict(area="master", title="Update item master",
        text="Re-matches every open (not exported) invoice against the live item master in the background; activate and rollback start this too."),
    ("GET", "/api/item-master/refresh"): dict(area="master", title="Item master refresh result",
        text="The latest re-match: status, counts and, per changed invoice, the lines newly matched, lost or changed.", smoke=200),
    ("GET", "/api/mapping-tables"): dict(area="master", title="Mapping-table versions",
        text="Uploaded and active mapping-table versions.", smoke=200),
    ("POST", "/api/mapping-tables/validate"): dict(area="master", title="Validate mapping tables",
        text="Multipart file upload; validates without activating.", form={"file": "@mapping-tables.xlsx"}),
    ("POST", "/api/mapping-tables/uploads/{upload_id}/activate"): dict(area="master", title="Activate mapping tables",
        text="Makes a validated upload active."),
    ("POST", "/api/mapping-tables/versions/{version_id}/rollback"): dict(area="master",
        title="Roll back mapping tables", text="Returns to an earlier version."),
    ("GET", "/api/mapping-tables/active/workbook"): dict(area="master", title="Download active mapping tables",
        text="The active workbook."),
    # setup
    ("POST", "/api/references"): dict(area="setup", title="Import references",
        text="Multipart file upload of the reference workbook.", form={"file": "@reference.xlsx"}),
    ("POST", "/api/references/demo"): dict(area="setup", title="Load demo references", text="Synthetic references."),
    ("POST", "/api/demo"): dict(area="setup", title="Run the demo invoice", text="Reads the synthetic invoice "
        "under a confirmed plan.", body={"preflight_token": "{{planToken}}"}),
    ("POST", "/api/templates"): dict(area="setup", title="Add a supplier template",
        text="Multipart upload of an invoice2data YAML template.", form={"file": "@template.yml"}),
    ("POST", "/api/policy"): dict(area="setup", title="Save tolerances",
        text="Price and total tolerances and currency decimals. Marks unexported invoices for review again.",
        body={"price_tolerance": "0", "total_tolerance": "0.01", "currency_decimals": {"AED": 2}}),
    # ai
    ("POST", "/api/settings"): dict(area="ai", title="AI settings",
        text="Default provider and model, and the AI on/off switch (ai_fallback). With it off the server refuses "
             "every AI read.", body={"provider": "azure", "model": "", "ai_fallback": False}),
    ("GET", "/api/ai-rules"): dict(area="ai", title="Rules the AI added",
        text="The rules the AI added, each with its source, reason, model, file and the lines it has filled. There is no approval step."),
    ("DELETE", "/api/ai-rules/{rule_id}"): dict(area="ai", title="Remove a rule the AI added",
        text="Removes one rule the AI added, so it fills nothing more. Audited."),
    ("POST", "/api/connections/azure"): dict(area="ai", title="Connect Azure AI Foundry",
        text="Endpoint (an Azure resource host or Foundry project), agent or deployment name, and an api-key or managed "
             "identity. Makes a real call first: only a completed answer saves it, stored encrypted; a failed test saves "
             "nothing and keeps the previous connection.",
        body={"endpoint": "https://<resource>.openai.azure.com", "name": "<deployment or agent>", "auth": "key", "api_key": "<key>"}),
    ("DELETE", "/api/connections/azure"): dict(area="ai", title="Disconnect Azure AI Foundry",
        text="Removes the saved connection."),
    ("POST", "/api/connections/{provider}"): dict(area="ai", title="Connect OpenAI or Anthropic",
        text="Refused with 403 on the Azure deployment.", body={"api_key": "<key>"}),
    ("DELETE", "/api/connections/{provider}"): dict(area="ai", title="Disconnect OpenAI or Anthropic",
        text="Removes a saved key."),
    ("GET", "/api/connections/{provider}/models"): dict(area="ai", title="List models",
        text="Models of a connected provider. 403 for a provider the deployment does not allow."),
    ("POST", "/api/subscriptions/claude/import"): dict(area="ai", title="Import a Claude subscription",
        text="Local installs only in practice; 403 on the Azure deployment.", body={"setup_token": "<token>"}),
    ("DELETE", "/api/subscriptions/claude"): dict(area="ai", title="Remove the Claude subscription", text="Removes it."),
    ("POST", "/api/chatgpt/start"): dict(area="ai", title="ChatGPT sign-in (local only)",
        text="Refused in the cloud build.", body={"account_id": None}),
    ("POST", "/api/chatgpt/select"): dict(area="ai", title="Select a ChatGPT account",
        text="403 on the Azure deployment.", body={"id": "<account>"}),
    ("POST", "/api/chatgpt/export"): dict(area="ai", title="Export a ChatGPT credential bundle",
        text="Refused in the cloud build.", body={"id": "<account>"}),
    ("POST", "/api/chatgpt/import"): dict(area="ai", title="Import a ChatGPT credential bundle",
        text="403 on the Azure deployment.", body={"bundle": {}, "passphrase": "<passphrase>"}),
    ("DELETE", "/api/chatgpt/{aid}"): dict(area="ai", title="Remove a ChatGPT account", text="Removes it."),
    # admin
    ("POST", "/api/jobs/delete"): dict(area="admin", title="Delete invoices",
        text="Permanently deletes invoices and their files. Each item names the invoice and its current revision; "
             "needs confirm_permanent: true (a boolean). For an exported invoice a minimal record stays so it is still "
             "noticed if sent again: its key, line quantities, the file's SHA-256 and its invoice number.",
        body={"jobs": [{"id": "{{jobId}}", "revision": 1}], "confirm_permanent": True}),
    ("GET", "/api/costs"): dict(area="admin", title="AI spend", text="Daily and month-to-date AI spend. ?days=30.",
        query={"days": "30"}, smoke=200),
    ("GET", "/api/users"): dict(area="admin", title="Users", text="Everyone who has signed in, their role and last "
        "sign-in. Read-only: roles are granted in Entra ID.", smoke=200),
}

# Routes that exist but are not part of the API: the page itself, static files, and the local-only callback.
NOT_API = {("GET", "/"), ("GET", "/auth/callback")}
