// Unit tests for the pure parts of tools/apps-script.gs, run with: node tools/script_test.js
// Apps Script globals are stubbed just enough; all data below is synthetic.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs.readFileSync(path.join(__dirname, "apps-script.gs"), "utf8");

// ---- stubs -------------------------------------------------------------------------------
const props = { TOKEN: "app-token-app-token", ADMIN_TOKEN: "admin-token-admin-token" };
const tabs = {};            // name -> { rows: [[...]], formatted: bool }
let gmailDb = {};           // id -> { subject, from, internalDate, body, snippet }
let gmailQueries = [];

function fakeSheet(name) {
  const t = tabs[name] || (tabs[name] = { rows: [], formatted: 0, frozen: 0, validations: 0 });
  return {
    getLastRow: () => t.rows.length,
    appendRow: (r) => { t.rows.push(r.slice()); },
    getRange: (row, col, nrows, ncols) => ({
      getValues: () => t.rows.slice(row - 1, row - 1 + nrows).map((r) => r.slice(col - 1, col - 1 + ncols)),
      clearContent: () => { t.rows.length = row - 1; },
      setNumberFormat: () => {},
      setValues: (vals) => { vals.forEach((v, i) => { t.rows[row - 1 + i] = v.slice(); }); },
      setDataValidation: () => { t.validations++; },
      setFontWeight: () => {},
      createFilter: () => { t.filters = (t.filters || 0) + 1; return { setColumnFilterCriteria() { t.filterCriteria = true; return this; } }; },
    }),
    getFilter: () => (t.filters ? { remove() { t.filters--; } } : null),
    setFrozenRows: (n) => { t.frozen = n; },
    setConditionalFormatRules: () => { t.formatted++; },
    setColumnWidth: () => {},
  };
}
const ss = {
  getSheetByName: (n) => (tabs[n] ? fakeSheet(n) : null),
  insertSheet: (n) => fakeSheet(n),
  getSpreadsheetTimeZone: () => "America/Chicago",
  getName: () => "test",
  getSheets: () => [],
};
const builder = { requireValueInList() { return this; }, setAllowInvalid() { return this; }, setHelpText() { return this; }, build() { return {}; },
                  whenFormulaSatisfied() { return this; }, setBackground() { return this; }, setRanges() { return this; }, setHiddenValues() { return this; } };
const g = {
  SpreadsheetApp: { getActiveSpreadsheet: () => ss, newDataValidation: () => builder, newConditionalFormatRule: () => builder, newFilterCriteria: () => builder },
  PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => props[k] || null, setProperty: (k, v) => { props[k] = v; } }) },
  LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
  ContentService: { createTextOutput: (t) => ({ setMimeType: () => ({ text: t }) }), MimeType: { JSON: "json" } },
  Utilities: {
    sleep() {},
    formatDate: (d) => d.toISOString().slice(0, 10),
    newBlob: (bytes) => ({ getDataAsString: () => Buffer.from(bytes).toString("utf8") }),
    base64DecodeWebSafe: (s) => Array.from(Buffer.from(s, "base64url")),
    base64Decode: (s) => Array.from(Buffer.from(s, "base64")),
  },
  ScriptApp: { getProjectTriggers: () => [], newTrigger: () => ({ timeBased: () => ({ everyDays: () => ({ atHour: () => ({ create() {} }) }) }) }) },
  Logger: { log() {} },
  Gmail: { Users: { Messages: {
    list: (_me, params) => {
      gmailQueries.push(params.q);
      const ids = Object.keys(gmailDb).filter((id) => gmailDb[id].match(params.q));
      return { messages: ids.map((id) => ({ id })) };
    },
    get: (_me, id, opts) => {
      const m = gmailDb[id];
      const msg = { id, internalDate: String(m.internalDate), snippet: m.snippet || "", payload: { headers: [{ name: "From", value: m.from }, { name: "Subject", value: m.subject }] } };
      if (opts.format === "full") {
        msg.payload.parts = [{ mimeType: "text/plain", body: { data: Buffer.from(m.body || "").toString("base64url") } }];
        (m.attachments || []).forEach((a, i) => msg.payload.parts.push({ mimeType: a.mimeType, filename: a.filename, body: { size: a.bytes.length, attachmentId: id + "-att" + i } }));
      }
      return msg;
    },
    Attachments: { get: (_me, id, attId) => ({ data: Buffer.from(gmailDb[id].attachments[Number(attId.split("-att")[1])].bytes).toString("base64url") }) },
  } } },
  DriveApp: { getRootFolder: () => fakeFolder(driveRoot) },
};
// Minimal Drive: a folder is { name, folders: [], files: [] }; createFile returns the file record.
let driveRoot = { name: "My Drive", folders: [], files: [] };
function iter(arr) { let i = 0; return { hasNext: () => i < arr.length, next: () => arr[i++] }; }
function fakeFolder(f) {
  return {
    getFoldersByName: (n) => iter(f.folders.filter((x) => x.name === n).map(fakeFolder)),
    createFolder: (n) => { const nf = { name: n, folders: [], files: [] }; f.folders.push(nf); return fakeFolder(nf); },
    getFilesByName: (n) => iter(f.files.filter((x) => x.name === n && !x.trashed).map((x) => ({ getUrl: () => "url:" + x.name, setTrashed: (t) => { x.trashed = t; } }))),
    createFile: (blob) => { const rec = { name: blob.name, bytes: blob.bytes, mimeType: blob.mimeType }; f.files.push(rec); return { getUrl: () => "url:" + rec.name }; },
  };
}
g.Utilities.newBlob = (bytes, mimeType, name) => ({ getDataAsString: () => Buffer.from(bytes).toString("utf8"), bytes, mimeType, name });
Object.assign(global, g);
// Load the script into the global scope (top-level function declarations become globals).
(0, eval)(src);

let passed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log("ok   " + name); }
  catch (e) { console.log("FAIL " + name + "\n     " + (e && e.stack ? e.stack.split("\n").slice(0, 12).join("\n     ") : e)); process.exitCode = 1; }
}
function reset() { for (const k of Object.keys(tabs)) delete tabs[k]; gmailDb = {}; gmailQueries = []; delete props.LAST_BUDGET_SCAN; driveRoot = { name: "My Drive", folders: [], files: [] }; }

// ---- gmail → drive ---------------------------------------------------------------------------
test("fileAttachments saves PDFs into a folder path, skips duplicates, dry run writes nothing", () => {
  reset();
  const pdf = Array.from(Buffer.from("%PDF-1.4 synthetic invoice"));
  gmailDb.m1 = { from: "Shop <email.notification@example.com>", subject: "Your Invoice is Ready", internalDate: Date.UTC(2025, 4, 28), body: "invoice i27625 attached",
                 attachments: [{ filename: "Invoice_i27625.pdf", mimeType: "application/pdf", bytes: pdf }, { filename: "logo.png", mimeType: "image/png", bytes: [1, 2, 3] }] };
  const dry = fileAttachments("m1", "299 Bloom/2025", { dry: true });
  assert.deepStrictEqual([dry.ok, dry.dry, dry.saved.length, dry.saved[0].name, dry.created], [true, true, 1, "Invoice_i27625.pdf", []]);
  assert.strictEqual(driveRoot.folders.length, 0, "dry run must not create folders");

  const wet = fileAttachments("m1", "299 Bloom/2025", {});
  assert.deepStrictEqual(wet.created, ["299 Bloom", "299 Bloom/2025"]);
  assert.deepStrictEqual(wet.saved.map((s) => [s.name, s.size]), [["Invoice_i27625.pdf", pdf.length]]);
  const folder = driveRoot.folders[0].folders[0];
  assert.deepStrictEqual([folder.name, folder.files.length, folder.files[0].mimeType], ["2025", 1, "application/pdf"]);
  assert.deepStrictEqual(Array.from(folder.files[0].bytes), pdf, "bytes round-trip through base64url");

  const again = fileAttachments("m1", "299 Bloom/2025", {});
  assert.deepStrictEqual([again.saved.length, again.skipped.length, again.skipped[0].reason, again.created], [0, 1, "exists", []]);
  assert.strictEqual(folder.files.length, 1);

  const over = fileAttachments("m1", "299 Bloom/2025", { overwrite: true });
  assert.strictEqual(over.saved.length, 1);
  assert.deepStrictEqual(folder.files.map((f) => !!f.trashed), [true, false]);

  const all = fileAttachments("m1", "299 Bloom/2025", { all: true });
  assert.deepStrictEqual(all.saved.map((s) => s.name), ["logo.png"], "all=1 adds the non-PDF; the PDF is skipped as existing");
  assert.strictEqual(fileAttachments("", "x", {}).ok, false);
  assert.strictEqual(fileAttachments("m1", "x", { dry: true }).saved.length, 1);
  gmailDb.m2 = { from: "a@b", subject: "no files", internalDate: Date.UTC(2025, 4, 28), body: "", attachments: [] };
  assert.strictEqual(fileAttachments("m2", "299 Bloom/2025", {}).note, "no matching attachments");
});

// ---- parsers --------------------------------------------------------------------------------
test("parseChaseAlert reads amount and merchant", () => {
  assert.deepStrictEqual(parseChaseAlert("You made a $226.02 transaction with DD *DOORDASH TARGET"), { amount: 226.02, merchant: "DD *DOORDASH TARGET" });
  assert.deepStrictEqual(parseChaseAlert("You made a $1,234.56 transaction with Amazon.com"), { amount: 1234.56, merchant: "Amazon.com" });
  assert.strictEqual(parseChaseAlert("Your credit card statement is available"), null);
});

test("parseZelle reads amount and recipient", () => {
  const z = parseZelle("Money Sent with Zelle® from Your Bank Account", "Hi, You sent $150.00 to Pat Example with Zelle® on 09/29/2026, from your account.");
  assert.deepStrictEqual(z, { amount: 150, recipient: "Pat Example" });
  const nb = parseZelle("Money Sent with Zelle\u00ae from Your Bank Account", "Hi, You sent $150.00\u00a0to Pat Example  with Zelle\u00ae on 09/29/2026");
  assert.deepStrictEqual(nb, { amount: 150, recipient: "Pat Example" });
  assert.strictEqual(parseZelle("Available Balance", "nothing here"), null);
});

test("parseAmazon splits a two-order confirmation and keeps the categories", () => {
  const meta = { subject: "Ordered 3 items: Household Supplies, Hand Tools, and more" };
  const body = "Order # 111-0000000-0000001 View or edit order https://x Grand Total: 6.48 USD Arriving today Order # 111-0000000-0000002 View Grand Total: 22.44 USD";
  const r = parseAmazon(meta, body);
  assert.strictEqual(r.length, 2);
  assert.deepStrictEqual([r[0].orderId, r[0].total, r[0].categories, r[0].kind], ["111-0000000-0000001", 6.48, "Household Supplies, Hand Tools", "amazon"]);
  assert.deepStrictEqual([r[1].orderId, r[1].total], ["111-0000000-0000002", 22.44]);
});

test("parseAmazon handles digital orders, refunds and unparsed bodies", () => {
  const d = parseAmazon({ subject: "Amazon.com order of Some Novel Title." }, "Order # D01-0000000-0000009 Order Total: $4.99");
  assert.deepStrictEqual([d[0].merchant, d[0].categories, d[0].items, d[0].total, d[0].orderId], ["Amazon digital", "Digital", "Some Novel Title", 4.99, "D01-0000000-0000009"]);
  const f = parseAmazon({ subject: "Advance refund issued for Widget Thing, Small..." }, "Your refund was issued. $4.49 will be credited to your Visa by Oct 3. View (https://x?orderId=111-0000000-0000003&y=1)");
  assert.deepStrictEqual([f[0].kind, f[0].total, f[0].orderId, f[0].items], ["amazon-refund", -4.49, "111-0000000-0000003", "Widget Thing, Small"]);
  const u = parseAmazon({ subject: "Ordered 1 item: Clothing" }, "nothing useful $12.00");
  assert.strictEqual(u[0].unparsed, true);
  assert.strictEqual(u[0].orderId, "");
  // Newer formats: item-named subject with "Total 27.8 USD", digital "Order #:" with "*Grand Total: $3.99", return logistics ignored
  const n = parseAmazon({ subject: 'Ordered: "Dust Bag Filters"' }, "Order # 111-0000000-0000011 View or edit order https://x * Dust Bag Filters Quantity: 1 Total 27.8 USD");
  assert.deepStrictEqual([n[0].orderId, n[0].total, n[0].items], ["111-0000000-0000011", 27.8, "Dust Bag Filters"]);
  const n2 = parseAmazon({ subject: 'Ordered: "Anova Culinary Sous Vide..." and 4 more items' }, "Order # 111-0000000-0000012 * Anova Culinary Sous Vide Quantity: 1 * Mason Jars Quantity: 2 Total 149.5 USD");
  assert.deepStrictEqual([n2[0].total, n2[0].items], [149.5, "Anova Culinary Sous Vide; 2x Mason Jars"]);
  const k = parseAmazon({ subject: "Amazon.com order of Some Novel Title." }, "Order Details Order #: D01-0000000-0000013 Placed on Friday Item Subtotal: $3.99 Total Before Tax: $3.99 Tax Collected: $0.00 *Grand Total: $3.99 *The grand total");
  assert.deepStrictEqual([k[0].orderId, k[0].total, k[0].items], ["D01-0000000-0000013", 3.99, "Some Novel Title"]);
  assert.deepStrictEqual(parseAmazon({ subject: "Dropoff confirmed for Telescope Bag..." }, "Your return was dropped off."), []);
  assert.deepStrictEqual(parseAmazon({ subject: "Return request confirmed for Telescope..." }, "x"), []);
  assert.deepStrictEqual(parseAmazon({ subject: "Your return drop off confirmation for Chain..." }, "x"), []);
  assert.deepStrictEqual(parseAmazon({ subject: "Refund ineligible for Shapewear...." }, "x"), []);
  assert.deepStrictEqual(parseAmazon({ subject: "Pat sent you printing information about an Amazon.com return" }, "x"), []);
  const long = parseAmazon({ subject: 'Ordered: "Shoes..." and 5 more items' }, "Order # 111-0000000-0000014 " + "* Item number x Quantity: 1 ".repeat(6) + "x".repeat(1200) + " Total 203.4 USD");
  assert.deepStrictEqual([long[0].orderId, long[0].total], ["111-0000000-0000014", 203.4]);
  const f2 = parseAmazon({ subject: "Advance refund issued for Thread Spool...." }, "Return summary Refund subtotal $7.29 Total refund* $7.29 orderId=111-0000000-0000003");
  assert.deepStrictEqual([f2[0].kind, f2[0].total], ["amazon-refund", -7.29]);
});

test("parseDoorDash reads merchant, total, last4 and items; order confirmations are a different kind", () => {
  const body = "Paid with Visa Ending in 1234 and/or credits Target Total: $225.33 Your receipt Items that were adjusted Substituted 1x Ground Nutmeg (2.12 oz) $4.69 Substituted with: 1x Allspice (2 oz) $3.19 2x Chicken Tenderloins $12.99";
  const r = parseDoorDash({ subject: "Final receipt for Beck from Target" }, body);
  assert.deepStrictEqual([r.kind, r.merchant, r.total, r.last4], ["doordash", "Target", 225.33, "1234"]);
  assert.ok(r.items.includes("1x Ground Nutmeg (2.12 oz) $4.69") && r.items.includes("2x Chicken Tenderloins $12.99"));
  const o = parseDoorDash({ subject: "Order Confirmation for Beck from Panera Bread" }, "Paid with Visa Ending in 1234 and/or credits Panera Bread Total: $87.84 Your receipt Estimated Total $87.84");
  assert.deepStrictEqual([o.kind, o.merchant, o.total, o.items], ["doordash-order", "Panera Bread", 87.84, ""]);
});

test("parseUber reads the total and card, never the addresses", () => {
  const r = parseUber({ subject: "Your Friday evening trip with Uber", snippet: "" }, "Thanks for riding Total $59.16 Trip fare $51.91 Payments Apple Pay Visa ••••9995 $59.16 Trip details 1 Some St, Chicago");
  assert.deepStrictEqual([r.kind, r.merchant, r.total, r.last4, r.items], ["uber", "Uber ride", 59.16, "9995", "Friday evening"]);
  assert.ok(!JSON.stringify(r).includes("Some St"));
});

// ---- codecs -----------------------------------------------------------------------------------
test("transactions round-trip through the Sheet and reject bad tags", () => {
  reset();
  const v = normalizeTx({ date: "2026-09-30", merchant: "AMAZON MKTPLACE PMTS", amount: "44.64", account: "Chase", category: "Amazon", tag: "Frivolous", source: "alert", csv: "TRUE", gmailId: "abc", u: "2026-10-01T00:00:00.000Z" });
  assert.deepStrictEqual([v.amount, v.account, v.category, v.tag, v.csv], [44.64, "chase", "amazon", "frivolous", true]);
  const row = txToRow("e-abc", v);
  assert.strictEqual(row.length, COLLECTIONS.transactions.header.length);
  const back = txFromRow(row, { ids: {}, unreadable: [], now: "now" });
  assert.deepStrictEqual(back[1], v);
  const ctx = { ids: {}, unreadable: [], now: "now" };
  assert.strictEqual(txFromRow(["x", "2026-09-30", "M", "1", "chase", "", "", "maybe", "", "", "", "", "", "", "", ""], ctx), null);
  assert.deepStrictEqual(ctx.unreadable, ["x (tag 'maybe')"]);
  assert.strictEqual(normalizeTx({ date: "10/01/2026", amount: 1 }), null);
  assert.strictEqual(normalizeTx({ date: "2026-09-30", merchant: "m", amount: 1, tag: "bogus" }).tag, "");
});

test("receipts and budget rows round-trip", () => {
  const r = normalizeReceipt({ kind: "doordash", date: "2026-10-04", merchant: "Target", total: "225.33", items: "1x A $1.00", last4: "1234", gmailId: "g1", u: "2026-10-04T10:00:00.000Z" });
  assert.deepStrictEqual(receiptFromRow(receiptToRow("r-g1", r), { ids: {}, unreadable: [], now: "n" })[1], r);
  assert.strictEqual(normalizeReceipt({ kind: "lyft", date: "2026-10-04" }), null);
  const b = normalizeBudget({ target: "450", note: "groceries incl. Target runs", u: "2026-10-04T10:00:00.000Z" });
  assert.deepStrictEqual(budgetFromRow(budgetToRow("groceries", b), { ids: {}, unreadable: [], now: "n" }), ["groceries", b]);
});

// ---- merge protection -------------------------------------------------------------------------
test("merge keeps Beck's filled tag/note/category and fills empty ones", () => {
  const cur = { "e-1": { date: "2026-09-30", merchant: "M", amount: 10, account: "chase", category: "dining", suggested: "", tag: "necessary", note: "work lunch", detail: "", billId: "", source: "alert", csv: false, gmailId: "1", u: "2026-10-01T00:00:00.000Z" },
                "e-2": { date: "2026-09-30", merchant: "N", amount: 5, account: "chase", category: "", suggested: "", tag: "", note: "", detail: "", billId: "", source: "alert", csv: false, gmailId: "2", u: "2026-10-01T00:00:00.000Z" } };
  const inc = { "e-1": Object.assign({}, cur["e-1"], { category: "restaurants", suggested: "unnecessary", tag: "frivolous", note: "", csv: true, u: "2026-10-02T00:00:00.000Z" }),
                "e-2": Object.assign({}, cur["e-2"], { category: "amazon", suggested: "necessary", u: "2026-10-02T00:00:00.000Z" }) };
  const m = merge("transactions", cur, inc);
  assert.deepStrictEqual([m["e-1"].tag, m["e-1"].note, m["e-1"].category, m["e-1"].suggested, m["e-1"].csv], ["necessary", "work lunch", "dining", "unnecessary", true]);
  assert.deepStrictEqual([m["e-2"].category, m["e-2"].suggested], ["amazon", "necessary"]);
});

test("merge: ties keep the Sheet row, tombstones win when newer, owned fields never resurrect a deleted row", () => {
  const cur = { "e-1": { date: "2026-09-30", merchant: "M", amount: 10, account: "chase", category: "", suggested: "", tag: "skip", note: "", detail: "", billId: "", source: "alert", csv: false, gmailId: "1", u: "2026-10-01T00:00:00.000Z" } };
  const same = merge("transactions", cur, { "e-1": Object.assign({}, cur["e-1"], { suggested: "x" }) });
  assert.strictEqual(same["e-1"].suggested, "");
  const del = merge("transactions", cur, { "e-1": { del: true, u: "2026-10-03T00:00:00.000Z" } });
  assert.strictEqual(del["e-1"].del, true);
  const budget = merge("budget", { groceries: { target: 500, note: "", u: "2026-10-01T00:00:00.000Z" } }, { groceries: { target: 400, note: "proposed", u: "2026-10-02T00:00:00.000Z" } });
  assert.deepStrictEqual([budget.groceries.target, budget.groceries.note], [500, "proposed"]);
});

// ---- sheet io ---------------------------------------------------------------------------------
test("writeAll orders transactions newest first and formats the tab once", () => {
  reset();
  writeAll("transactions", {
    "e-a": normalizeTx({ date: "2026-09-01", merchant: "A", amount: 1, u: "2026-10-01T00:00:00.000Z" }),
    "e-b": normalizeTx({ date: "2026-09-30", merchant: "B", amount: 2, u: "2026-10-01T00:00:00.000Z" }),
    "e-c": normalizeTx({ date: "2026-09-30", merchant: "C", amount: 3, u: "2026-10-01T00:00:00.000Z" }),
  });
  const t = tabs.transactions;
  assert.deepStrictEqual(t.rows.slice(1).map((r) => r[0]), ["e-b", "e-c", "e-a"]);
  assert.deepStrictEqual([t.frozen, t.validations, t.formatted, t.filters, t.filterCriteria], [1, 1, 1, 1, true]);
  const back = readAll("transactions");
  assert.strictEqual(Object.keys(back).length, 3);
  writeAll("transactions", back);
  assert.strictEqual(t.validations, 1, "setup must not run again on an existing tab");
});

test("GET without budget=1 is unchanged; with it the three tabs come back", () => {
  reset();
  const plain = JSON.parse(doGet({ parameter: { token: props.TOKEN } }).text);
  assert.deepStrictEqual(Object.keys(plain).sort(), ["bills", "lastScan", "log", "ok", "payments", "version"]);
  const full = JSON.parse(doGet({ parameter: { token: props.TOKEN, budget: "1" } }).text);
  assert.ok("transactions" in full && "receipts" in full && "budget" in full && "lastBudgetScan" in full);
});

test("POST merges budget collections only when sent, and quiet returns counts", () => {
  reset();
  const body = { token: props.TOKEN, transactions: { "c-1": { date: "2026-09-01", merchant: "JEWEL", amount: 80, account: "chase", source: "csv", u: "2026-10-01T00:00:00.000Z" } }, quiet: true };
  const r = JSON.parse(doPost({ postData: { contents: JSON.stringify(body) } }).text);
  assert.deepStrictEqual(r.transactions, { rows: 1 });
  assert.ok(!("receipts" in r) && !("budget" in r));
  // force: only with the admin token, and then Sheet-owned columns are replaced for the rows sent
  const tagged = { token: props.TOKEN, transactions: { "c-1": { date: "2026-09-01", merchant: "JEWEL", amount: 80, account: "chase", source: "csv", tag: "skip", u: "2026-10-02T00:00:00.000Z" } } };
  doPost({ postData: { contents: JSON.stringify(tagged) } });
  assert.strictEqual(readAll("transactions")["c-1"].tag, "skip");
  const plain = JSON.parse(doPost({ postData: { contents: JSON.stringify(Object.assign({}, tagged, { transactions: { "c-1": Object.assign({}, tagged.transactions["c-1"], { tag: "necessary", u: "2026-10-03T00:00:00.000Z" }) } })) } }).text);
  assert.strictEqual(readAll("transactions")["c-1"].tag, "skip", "without force the Sheet keeps its tag");
  const noAdmin = JSON.parse(doPost({ postData: { contents: JSON.stringify(Object.assign({}, tagged, { force: true })) } }).text);
  assert.strictEqual(noAdmin.ok, false);
  doPost({ postData: { contents: JSON.stringify(Object.assign({}, tagged, { force: true, admin: props.ADMIN_TOKEN, transactions: { "c-1": Object.assign({}, tagged.transactions["c-1"], { tag: "necessary", u: "2026-10-04T00:00:00.000Z" }) } })) } });
  assert.strictEqual(readAll("transactions")["c-1"].tag, "necessary", "force with the admin token replaces the tag");
  const again = JSON.parse(doPost({ postData: { contents: JSON.stringify({ token: props.TOKEN, log: {} }) } }).text);
  assert.ok(!("transactions" in again));
  assert.strictEqual(readAll("transactions")["c-1"].merchant, "JEWEL");
});

// ---- scanBudget with a stubbed mailbox ---------------------------------------------------------
function mail(id, from, subject, date, body, extra) {
  gmailDb[id] = Object.assign({ from, subject, internalDate: new Date(date + "T12:00:00Z").getTime(), body: body || "", snippet: (body || "").slice(0, 100),
    match(q) { return q.indexOf(this.kind) >= 0; } }, extra);
}
test("scanBudget harvests alerts and receipts, skips what it has seen, writes under dry=false only", () => {
  reset();
  mail("c1", "Chase <no.reply.alerts@chase.com>", "You made a $87.84 transaction with DD *DOORDASH PANERAB", "2026-10-04", "", { kind: "chase.com" });
  mail("c2", "Chase <no.reply.alerts@chase.com>", "Your credit card statement is available", "2026-10-04", "", { kind: "chase.com" });
  mail("z1", "USAA <x@mailcenter.usaa.com>", "Money Sent with Zelle® from Your Bank Account", "2026-09-30", "You sent $150.00 to Pat Example with Zelle® on 09/30/2026", { kind: "mailcenter.usaa.com" });
  mail("a1", "Amazon.com <auto-confirm@amazon.com>", "Ordered 2 items: Plumbing, Home Safety", "2026-09-29", "Order # 111-0000000-0000001 Grand Total: 40.10 USD Order # 111-0000000-0000002 Grand Total: 54.10 USD", { kind: "amazon.com" });
  mail("d1", "DoorDash <no-reply@doordash.com>", "Final receipt for Beck from Panera Bread", "2026-10-04", "Paid with Visa Ending in 1234 Panera Bread Total: $87.84 1x Soup $8.99", { kind: "doordash.com" });
  mail("u1", "Uber Receipts <noreply@uber.com>", "Your Friday evening trip with Uber", "2026-09-26", "Total $59.16 Visa ••••9995 $59.16", { kind: "uber.com" });

  const dry = scanBudget({ kind: "all", days: 40, dry: true });
  assert.strictEqual(dry.ok, true);
  assert.strictEqual(dry.added.length, 6, JSON.stringify(dry.added));
  assert.ok(dry.warnings.some((w) => w.startsWith("chase: unparsed subject")));
  assert.ok(!tabs.transactions || tabs.transactions.rows.length <= 1, "dry run must not write");

  const wet = scanBudget({ kind: "all", days: 40 });
  assert.strictEqual(wet.added.length, 6);
  const tx = readAll("transactions"), rc = readAll("receipts");
  assert.deepStrictEqual(Object.keys(tx).sort(), ["e-c1", "e-z1"]);
  assert.deepStrictEqual([tx["e-c1"].amount, tx["e-c1"].merchant, tx["e-c1"].account, tx["e-c1"].source], [87.84, "DD *DOORDASH PANERAB", "chase", "alert"]);
  assert.deepStrictEqual([tx["e-z1"].amount, tx["e-z1"].merchant, tx["e-z1"].account, tx["e-z1"].source], [150, "Zelle to Pat Example", "usaa", "zelle"]);
  assert.deepStrictEqual(Object.keys(rc).sort(), ["a-111-0000000-0000001", "a-111-0000000-0000002", "r-d1", "r-u1"]);
  assert.strictEqual(rc["r-d1"].total, 87.84);
  assert.ok(props.LAST_BUDGET_SCAN);

  const again = scanBudget({ kind: "chase,amazon", days: 40 });
  assert.strictEqual(again.added.length, 0);
  assert.strictEqual(again.skipped.dup, 2, "c1 + a1 already harvested (c2 is unparsed, so it is re-checked)");
  assert.deepStrictEqual(again.kinds, ["chase", "amazon"]);
  assert.strictEqual(scanBudget({ kind: "lyft" }).ok, false);
});

console.log(passed + " passed" + (process.exitCode ? ", with failures" : ""));
