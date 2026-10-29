// Offline UI behavior with DOM doubles; real app script, no network or browser dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function main() {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.hidden = false; this.textContent = ""; }
    set innerHTML(_) { throw Error("unsafe HTML sink"); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(type, handler) { this.listeners[type] = handler; }
    setAttribute() {}
    text() { return this.textContent + this.children.map((node) => node.text()).join(" "); }
  }
  const nodes = new Map();
  const node = (id) => { if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id); };
  const context = vm.createContext({
    document: { querySelector: node, querySelectorAll: () => [], createElement: (tag) => new Element(tag), addEventListener() {} },
    window: { matchMedia: () => ({ addEventListener() {} }) },
    FormData: class { append() {} }, setTimeout, clearTimeout, URLSearchParams,
    location: { hash: "", search: "" },
  });
  const run = (source) => vm.runInContext(source, context);
  run(fs.readFileSync(process.argv[2], "utf8"));
  run("applySidebarMode = () => {}; setupPaneResizers = () => {}; setupModelPicker = () => {}; bindEvents();");
  const hostileName = '<img src=x onerror="boom">.xlsx';
  const report = { valid: true, sheets: { LOCATIONS: { rows: 1, accepted: 1, excluded: 0, flagged: 0 } }, flagged: [] };
  const fixture = {
    active: { id: "v1", activated_at: "today", activated_by: "owner" },
    versions: [{ id: "v1", filename: hostileName, created_at: "today", created_by: "owner", version: "mapping-v1", has_workbook: true, report },
               { id: "old", filename: "Previous", created_at: "yesterday", created_by: "owner", version: "old", has_workbook: false, report: null }],
  };
  context.fixture = fixture;
  const requests = [];
  context.handleApi = async (path, options = {}) => {
    requests.push([path, options.method]);
    if (path.endsWith("/validate")) return { upload_id: "pending", filename: hostileName, report };
    return fixture;
  };
  run("api = handleApi; notify = () => {}; loadState = async () => {}; app.authMode = 'cloud'; app.sessionUser = {role: 'member'};");
  await run("loadMappingTables()");
  assert.equal(requests.length, 0, "member must not request admin history");
  run("app.sessionUser = {role: 'admin'};");
  await run("loadMappingTables()");
  assert(node("#mapping-history").text().includes(hostileName));
  assert(node("#mapping-active").text().includes(hostileName));
  assert.equal(node("#mapping-download").hidden, false);
  node("#mapping-file").files = [{ name: "test.xlsx", size: 10 }];
  await node("#mapping-upload-form").listeners.submit({ preventDefault() {} });
  assert.equal(node("#mapping-activate").hidden, false);
  assert(node("#mapping-report").text().includes("Validation passed"));
  assert(node("#mapping-report").text().includes(hostileName));
  await run("changeMappingVersion('/api/mapping-tables/uploads/pending/activate', $('#mapping-activate'))");
  assert(requests.some(([path, method]) => path.endsWith("/pending/activate") && method === "POST"));
  assert.equal(node("#mapping-activate").hidden, true);
  const oldEntry = node("#mapping-history").children[1];
  const button = oldEntry.children.find((child) => child.tagName === "button");
  button.listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert(requests.some(([path, method]) => path.endsWith("/old/rollback") && method === "POST"));
  report.valid = false; report.flagged = [{ sheet: "LOCATIONS", row: 2, reason: "location id repeats" }];
  context.handleApi = async () => ({ upload_id: null, filename: hostileName, report });
  run("api = handleApi");
  await node("#mapping-upload-form").listeners.submit({ preventDefault() {} });
  assert.equal(node("#mapping-activate").hidden, true);
  assert(node("#mapping-report").text().includes("LOCATIONS · row 2: location id repeats"));
  context.handleApi = async () => { throw Error("offline"); }; run("api = handleApi");
  await node("#mapping-upload-form").listeners.submit({ preventDefault() {} });
  assert.equal(node("#mapping-validate").disabled, false);
  assert.equal(node("#mapping-file").disabled, false);
  assert.equal(node("#mapping-activate").hidden, true);
  run("app.authMode = 'local'; app.sessionUser = null; api = async () => fixture;");
  await run("loadMappingTables()");
  assert(node("#mapping-history").text().includes(hostileName));
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
