/**
 * Home Logbook — Google Apps Script web app (v3).
 *
 * Bound to the "Home Logbook progress" Sheet. Three tabs, created on first use:
 *   log       id | last | history | updated | deleted                    (task tick-offs)
 *   bills     id | name | category | cadence | dueDay | dueMonths | amount | autopay | sender |
 *             subjectPaid | subjectBill | amountRegex | payUrl | notes | active |
 *             lastBillAmount | lastBillDue | updated | deleted
 *   payments  id | billId | date | amount | source | gmailId | subject | updated | deleted
 *
 * Setup (once):
 *   1. Script properties: TOKEN (the app's token) and ADMIN_TOKEN (different; only used from
 *      the owner's machine, never from the page).
 *   2. Project Settings → "Show appsscript.json" → paste tools/appsscript.json (Gmail read-only
 *      scope + the Gmail advanced service).
 *   3. Run installTrigger() from the editor once: approves the scopes and schedules the daily
 *      email scan at 06:00.
 *   4. Deploy → New deployment → Web app → Execute as Me → Anyone. Later changes: Deploy →
 *      Manage deployments → pencil → Version: New (keeps the same URL).
 *
 * Merge rule for every collection: per id, the record with the newest "u" (ISO time) wins; a
 * {del:true,u} tombstone wins when newer. A write is refused if any row cannot be parsed, so a
 * typo in the Sheet can never cause rows to be dropped.
 */

var VERSION = 5;
var CADENCES = ["weekly", "monthly", "quarterly", "semiannual", "annual"];
var SCAN_DAYS = 40;             // default look-back for the daily / on-demand scan
var SCAN_MIN_GAP_MS = 5 * 60 * 1000;

var COLLECTIONS = {
  log: {
    header: ["id", "last", "history", "updated", "deleted"],
    fromRow: logFromRow, toRow: logToRow, normalize: normalizeLog
  },
  bills: {
    header: ["id", "name", "category", "cadence", "dueDay", "dueMonths", "amount", "autopay", "sender",
             "subjectPaid", "subjectBill", "amountRegex", "payUrl", "notes", "active",
             "lastBillAmount", "lastBillDue", "updated", "deleted"],
    fromRow: billFromRow, toRow: billToRow, normalize: normalizeBill
  },
  payments: {
    header: ["id", "billId", "date", "amount", "source", "gmailId", "subject", "updated", "deleted"],
    fromRow: paymentFromRow, toRow: paymentToRow, normalize: normalizePayment
  }
};
var NAMES = ["log", "bills", "payments"];

/* ------------------------------------------------------------------ web app */

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.admin !== undefined) {
    // Owner-only operations. Never consult the app token here.
    if (!adminAuthorized(p.admin)) return out({ ok: false, error: "unauthorized" });
    try {
      if (p.op === "discover") return out(discoverGmail(clampInt(p.months, 1, 36, 12), clampInt(p.max, 50, 600, 400), gmailDate(p.after), gmailDate(p.before)));
      if (p.op === "raw") return out(rawMessage(String(p.id || "")));
      if (p.op === "scan") return out(scanGmail({ months: p.months ? clampInt(p.months, 1, 24, 12) : null, days: SCAN_DAYS, bill: p.bill || null, dry: p.dry === "1" }));
      if (p.op === "peek") return out(peekMessage(String(p.id || "")));
      return out({ ok: false, error: "unknown op" });
    } catch (err) {
      return out({ ok: false, error: String(err) });
    }
  }
  if (!authorized(p.token)) return out({ ok: false, error: "unauthorized" });
  var resp = { ok: true, version: VERSION, lastScan: getProp("LAST_SCAN") || "" };
  for (var i = 0; i < NAMES.length; i++) resp[NAMES[i]] = readAll(NAMES[i]);
  return out(resp);
}

function doPost(e) {
  var body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out({ ok: false, error: "bad json" }); }
  if (!authorized(body.token)) return out({ ok: false, error: "unauthorized" });

  var scanInfo = null;
  if (body.scan) {
    try { scanInfo = scanIfDue(); } catch (err) { scanInfo = { error: String(err) }; }
  }

  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var current = {}, i, name;
    for (i = 0; i < NAMES.length; i++) {
      name = NAMES[i];
      current[name] = readAll(name);
      if (current[name].__unreadable) {
        return out({ ok: false, error: "unreadable " + name + " rows: " + current[name].__unreadable.join(", ") });
      }
    }
    var merged = {};
    merged.log = merge("log", current.log, body.log || {});
    merged.bills = merge("bills", current.bills, body.bills || {});
    merged.payments = merge("payments", current.payments, body.payments || {});
    // Write only the collections the client sent, so an older app never rewrites the others.
    for (i = 0; i < NAMES.length; i++) {
      name = NAMES[i];
      if (body[name] && typeof body[name] === "object") writeAll(name, merged[name]);
    }
    var resp = { ok: true, version: VERSION, lastScan: getProp("LAST_SCAN") || "" };
    for (i = 0; i < NAMES.length; i++) resp[NAMES[i]] = merged[NAMES[i]];
    if (scanInfo) resp.scan = scanInfo;
    return out(resp);
  } finally {
    lock.releaseLock();
  }
}

function authorized(token) {
  var want = getProp("TOKEN");
  return !!want && typeof token === "string" && token === want;
}

function adminAuthorized(token) {
  var want = getProp("ADMIN_TOKEN"), app = getProp("TOKEN");
  return !!want && want.length >= 16 && want !== app && typeof token === "string" && token === want;
}

function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/* ------------------------------------------------------------------ sheet io */

function sheet(name) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (sh.getLastRow() === 0) sh.appendRow(COLLECTIONS[name].header);
  return sh;
}

function readAll(name) {
  var c = COLLECTIONS[name];
  var sh = sheet(name);
  var last = sh.getLastRow();
  var obj = {};
  if (last < 2) return obj;
  var rows = sh.getRange(2, 1, last - 1, c.header.length).getValues();
  var ctx = { ids: {}, unreadable: [], now: new Date().toISOString() };
  rows.forEach(function (r) {
    var rec = c.fromRow(r, ctx);
    if (rec) { obj[rec[0]] = rec[1]; ctx.ids[rec[0]] = true; }
  });
  if (ctx.unreadable.length) obj.__unreadable = ctx.unreadable;
  return obj;
}

function writeAll(name, obj) {
  var c = COLLECTIONS[name];
  var sh = sheet(name);
  var ids = Object.keys(obj).filter(function (k) { return k.indexOf("__") !== 0; }).sort();
  var rows = ids.map(function (id) { return c.toRow(id, obj[id]); });
  var lastRow = sh.getLastRow();
  if (lastRow > 1) sh.getRange(2, 1, lastRow - 1, c.header.length).clearContent();
  if (rows.length) {
    var range = sh.getRange(2, 1, rows.length, c.header.length);
    range.setNumberFormat("@");   // plain text: stop Sheets turning dates and numbers into typed cells
    range.setValues(rows);
  }
}

function merge(name, current, incoming) {
  var normalize = COLLECTIONS[name].normalize;
  var merged = {}, id;
  for (id in current) if (id.indexOf("__") !== 0) merged[id] = current[id];
  for (id in incoming) {
    if (id.indexOf("__") === 0) continue;
    var v = normalize(incoming[id]);
    if (!v) continue;
    var cur = merged[id];
    if (!cur || stamp(v) > stamp(cur)) merged[id] = v;   // strict: a tie keeps the Sheet's row
  }
  return merged;
}

function stamp(v) { return v && v.u ? String(v.u) : ""; }

/* ------------------------------------------------------------------ log */

function logFromRow(r, ctx) {
  var id = s(r[0]);
  if (!id) return null;
  var updated = isoOf(r[3]);
  if (s(r[4]).toUpperCase() === "TRUE") return [id, { del: true, u: updated }];
  var hist = isDateObj(r[2]) ? [dateStr(r[2])]
    : s(r[2]).split(",").map(function (x) { return dateStr(x); }).filter(isDate);
  var lastDate = isDate(dateStr(r[1])) ? dateStr(r[1]) : (hist.length ? hist[hist.length - 1] : "");
  if (!lastDate) { ctx.unreadable.push(id); return null; }
  if (hist.indexOf(lastDate) < 0) hist.push(lastDate);
  hist = uniq(hist).sort();
  return [id, { last: lastDate, history: hist.slice(-12), u: updated || (lastDate + "T00:00:00Z") }];
}

function logToRow(id, v) {
  if (v.del) return [id, "", "", v.u || "", "TRUE"];
  return [id, v.last, (v.history || [v.last]).join(","), v.u || "", ""];
}

function normalizeLog(v) {
  if (!v || typeof v !== "object") return null;
  if (v.del) return { del: true, u: isoOf(v.u) || new Date().toISOString() };
  if (!isDate(v.last)) return null;
  var hist = (Array.isArray(v.history) ? v.history : [v.last]).filter(isDate);
  if (hist.indexOf(v.last) < 0) hist.push(v.last);
  hist = uniq(hist).sort().slice(-12);
  return { last: hist[hist.length - 1], history: hist, u: isoOf(v.u) || (v.last + "T00:00:00Z") };
}

/* ------------------------------------------------------------------ bills */

function billFromRow(r, ctx) {
  var id = s(r[0]), name = s(r[1]);
  if (!id && !name) return null;
  if (!id) id = uniqueSlug(name, ctx.ids);
  var updated = isoOf(r[17]) || ctx.now;   // a hand-typed row with no stamp counts as "just edited"
  if (s(r[18]).toUpperCase() === "TRUE") return [id, { del: true, u: updated }];
  var cadence = s(r[3]).toLowerCase() || "monthly";
  if (CADENCES.indexOf(cadence) < 0) { ctx.unreadable.push(id + " (cadence '" + cadence + "')"); return null; }
  var dueMonths = monthsList(r[5]);
  if (cadence !== "monthly" && cadence !== "weekly" && !dueMonths.length) { ctx.unreadable.push(id + " (dueMonths)"); return null; }
  return [id, {
    name: name || id, category: s(r[2]), cadence: cadence,
    dueDay: clampInt(r[4], 1, 31, 1), dueMonths: dueMonths, amount: toNum(r[6]), autopay: toBool(r[7]),
    sender: s(r[8]), subjectPaid: s(r[9]), subjectBill: s(r[10]), amountRegex: s(r[11]),
    payUrl: s(r[12]), notes: s(r[13]), active: s(r[14]).toUpperCase() !== "FALSE",
    lastBillAmount: toNum(r[15]), lastBillDue: isDate(dateStr(r[16])) ? dateStr(r[16]) : "",
    u: updated
  }];
}

function billToRow(id, v) {
  if (v.del) return [id, "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", v.u || "", "TRUE"];
  return [id, v.name || "", v.category || "", v.cadence || "monthly", String(v.dueDay || 1),
    (v.dueMonths || []).join(","), numStr(v.amount), v.autopay ? "TRUE" : "", v.sender || "",
    v.subjectPaid || "", v.subjectBill || "", v.amountRegex || "", v.payUrl || "", v.notes || "",
    v.active === false ? "FALSE" : "", numStr(v.lastBillAmount), v.lastBillDue || "", v.u || "", ""];
}

function normalizeBill(v) {
  if (!v || typeof v !== "object") return null;
  var now = new Date().toISOString();
  if (v.del) return { del: true, u: isoOf(v.u) || now };
  var name = s(v.name);
  if (!name) return null;
  var cadence = s(v.cadence).toLowerCase() || "monthly";
  if (CADENCES.indexOf(cadence) < 0) return null;
  var dueMonths = monthsList(Array.isArray(v.dueMonths) ? v.dueMonths.join(",") : v.dueMonths);
  if (cadence !== "monthly" && cadence !== "weekly" && !dueMonths.length) return null;
  return {
    name: name, category: s(v.category), cadence: cadence,
    dueDay: clampInt(v.dueDay, 1, 31, 1), dueMonths: dueMonths, amount: toNum(v.amount), autopay: toBool(v.autopay),
    sender: s(v.sender), subjectPaid: s(v.subjectPaid), subjectBill: s(v.subjectBill), amountRegex: s(v.amountRegex),
    payUrl: s(v.payUrl), notes: s(v.notes), active: v.active !== false && s(v.active).toUpperCase() !== "FALSE",
    lastBillAmount: toNum(v.lastBillAmount), lastBillDue: isDate(s(v.lastBillDue)) ? s(v.lastBillDue) : "",
    u: isoOf(v.u) || now
  };
}

/* ------------------------------------------------------------------ payments */

function paymentFromRow(r, ctx) {
  var id = s(r[0]), billId = s(r[1]), date = dateStr(r[2]);
  if (!id && !billId && !date) return null;
  if (!id) id = "m-" + billId + "-" + date;   // a row Beck typed by hand
  var updated = isoOf(r[7]) || ctx.now;
  if (s(r[8]).toUpperCase() === "TRUE") return [id, { del: true, u: updated }];
  if (!billId || !isDate(date)) { ctx.unreadable.push(id); return null; }
  return [id, { billId: billId, date: date, amount: toNum(r[3]), source: s(r[4]) || "manual",
                gmailId: s(r[5]), subject: s(r[6]).slice(0, 120), u: updated }];
}

function paymentToRow(id, v) {
  if (v.del) return [id, "", "", "", "", "", "", v.u || "", "TRUE"];
  return [id, v.billId, v.date, numStr(v.amount), v.source || "manual", v.gmailId || "", v.subject || "", v.u || "", ""];
}

function normalizePayment(v) {
  if (!v || typeof v !== "object") return null;
  var now = new Date().toISOString();
  if (v.del) return { del: true, u: isoOf(v.u) || now };
  var billId = s(v.billId), date = s(v.date);
  if (!billId || !isDate(date)) return null;
  var source = s(v.source).toLowerCase();
  return { billId: billId, date: date, amount: toNum(v.amount), source: source === "email" ? "email" : "manual",
           gmailId: s(v.gmailId), subject: s(v.subject).slice(0, 120), u: isoOf(v.u) || now };
}

/* ------------------------------------------------------------------ gmail: classification */

var PAY_RE  = /payment (received|confirmation|confirmed|processed|posted|successful|complete|was made|has been made)|thank you for your payment|thanks for your payment|we received your payment|we('ve| have) received your|you(?:'ve| have)? paid|has been paid|auto\s?pay(?:ment)? (processed|complete|posted|was made)|receipt for|payment receipt|your receipt|order confirmation|charged|successfully charged/i;
var BILL_RE = /statement (is )?(ready|available)|bill is (ready|available|due)|new (bill|statement|invoice)|your (bill|invoice|statement)|amount due|is due|upcoming (payment|bill|charge)|auto\s?pay(?:ment)? (is )?scheduled|will be (charged|debited|drafted)|premium (notice|due)|renewal|payment reminder|payment due/i;
var CONTEXT_RE = /(?:amount|total|payment|paid|charged|due|balance)[^$\n]{0,40}\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)/i;
var FIRST_DOLLAR_RE = /\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)/;
var DUE_DATE_RE = /due(?: date| on| by)?[^\n]{0,25}?((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}|\d{1,2}\/\d{1,2}\/\d{2,4})/i;

function safeRe(src, warnings, label) {
  if (!src) return null;
  try { return new RegExp(src, "i"); } catch (e) { if (warnings) warnings.push(label + ": bad regex " + src); return null; }
}

function classify(bill, meta, warnings) {
  var text = (meta.subject || "") + " " + (meta.snippet || "");
  var payRe = safeRe(bill.subjectPaid, warnings, bill.name + " subjectPaid") || PAY_RE;
  var billRe = safeRe(bill.subjectBill, warnings, bill.name + " subjectBill") || BILL_RE;
  if (payRe.test(text)) return "PAYMENT";
  if (billRe.test(text)) return "BILL";
  return null;
}

function extractAmount(text, customSrc, warnings) {
  var m = null;
  var custom = safeRe(customSrc, warnings, "amountRegex");
  if (custom) m = custom.exec(text);
  if (!m) m = CONTEXT_RE.exec(text);
  if (!m) m = FIRST_DOLLAR_RE.exec(text);
  if (!m || !m[1]) return null;
  var n = Number(String(m[1]).replace(/,/g, ""));
  return isNaN(n) ? null : n;
}

function extractDueDate(text) {
  var m = DUE_DATE_RE.exec(text);
  return m ? parseLooseDate(m[1]) : "";
}

function parseLooseDate(str) {
  var MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  var m = /^([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})$/i.exec(str.trim());
  var y, mo, d;
  if (m) { mo = MONTHS[m[1].toLowerCase()]; d = Number(m[2]); y = Number(m[3]); }
  else {
    m = /^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/.exec(str.trim());
    if (!m) return "";
    mo = Number(m[1]); d = Number(m[2]); y = Number(m[3]); if (y < 100) y += 2000;
  }
  if (!mo || !d || mo > 12 || d > 31) return "";
  return y + "-" + pad2(mo) + "-" + pad2(d);
}

/* ------------------------------------------------------------------ gmail: access */

// Gmail API quota is per minute per user. Pace calls and back off on quota errors.
var gmailCalls = 0;
function gmailCall(fn) {
  for (var attempt = 1; ; attempt++) {
    try {
      gmailCalls++;
      if (gmailCalls % 20 === 0) Utilities.sleep(600);
      return fn();
    } catch (err) {
      var msg = String(err);
      if (attempt >= 6 || !/quota|rate ?limit|backend ?error|too many/i.test(msg)) throw err;
      Utilities.sleep(2000 * attempt);
    }
  }
}

function gmailSearch(q, max) {
  var ids = [], token = null, res;
  do {
    var params = { q: q, maxResults: Math.min(max - ids.length, 100) };
    if (token) params.pageToken = token;
    res = gmailCall(function () { return Gmail.Users.Messages.list("me", params); });
    (res.messages || []).forEach(function (m) { ids.push(m.id); });
    token = res.nextPageToken;
  } while (token && ids.length < max);
  return ids.slice(0, max);
}

function headerOf(msg, name) {
  var hs = (msg.payload && msg.payload.headers) || [];
  for (var i = 0; i < hs.length; i++) if (String(hs[i].name).toLowerCase() === name.toLowerCase()) return hs[i].value || "";
  return "";
}

function metaOf(m) {
  var from = headerOf(m, "From");
  var date = new Date(Number(m.internalDate));
  return { id: m.id, from: from, domain: senderDomain(from), subject: headerOf(m, "Subject"),
           date: Utilities.formatDate(date, tz(), "yyyy-MM-dd"), snippet: decodeEntities(m.snippet || "") };
}

function gmailMeta(id) {
  return metaOf(gmailCall(function () { return Gmail.Users.Messages.get("me", id, { format: "metadata", metadataHeaders: ["From", "Subject", "Date"] }); }));
}

function gmailFull(id) {
  var m = gmailCall(function () { return Gmail.Users.Messages.get("me", id, { format: "full" }); });
  var meta = metaOf(m);
  meta.body = bodyText(m.payload).slice(0, 20000);
  return meta;
}

function bodyText(payload) {
  var plain = [], html = [];
  (function walk(p) {
    if (!p) return;
    var mt = String(p.mimeType || "").toLowerCase();
    if (p.body && p.body.data) {
      var txt = decodeB64(p.body.data);
      if (mt.indexOf("text/plain") === 0) plain.push(txt);
      else if (mt.indexOf("text/html") === 0) html.push(txt);
    }
    (p.parts || []).forEach(walk);
  })(payload);
  if (plain.length) return plain.join("\n");
  return stripHtml(html.join("\n"));
}

var lastDecodeError = "";
function decodeB64(data) {
  var str = String(data || "");
  try {
    var bytes = Utilities.base64DecodeWebSafe(str);
    var txt = Utilities.newBlob(bytes).getDataAsString("UTF-8");
    if (txt) return txt;
  } catch (e) { lastDecodeError = "websafe: " + e; }
  try {
    var std = str.replace(/-/g, "+").replace(/_/g, "/");
    while (std.length % 4) std += "=";
    return Utilities.newBlob(Utilities.base64Decode(std)).getDataAsString("UTF-8");
  } catch (e2) { lastDecodeError += " | std: " + e2; return ""; }
}

// Admin diagnostic: the MIME skeleton of one message and any decode error, no body text.
function rawMessage(id) {
  if (!id) return { ok: false, error: "id required" };
  var m = gmailCall(function () { return Gmail.Users.Messages.get("me", id, { format: "full" }); });
  function skel(p) {
    if (!p) return null;
    return { mimeType: p.mimeType, hasData: !!(p.body && p.body.data), size: p.body ? p.body.size : null,
             dataSample: p.body && p.body.data ? String(p.body.data).slice(0, 24) : "", parts: (p.parts || []).map(skel) };
  }
  lastDecodeError = "";
  var text = bodyText(m.payload);
  return { ok: true, id: id, subject: headerOf(m, "Subject"), skeleton: skel(m.payload), decoded: text.length, decodeError: lastDecodeError, sample: text.slice(0, 300) };
}

function stripHtml(h) {
  return decodeEntities(String(h)
    .replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>|<\/p>|<\/div>|<\/tr>|<\/li>|<\/h\d>/gi, "\n").replace(/<[^>]+>/g, " "))
    .replace(/[ \t ]+/g, " ").replace(/\n\s*\n+/g, "\n").trim();
}

function decodeEntities(x) {
  return String(x).replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'").replace(/&#(\d+);/g, function (_, n) { return String.fromCharCode(Number(n)); });
}

function senderDomain(from) {
  var m = /<([^>]+)>/.exec(from || "");
  var addr = (m ? m[1] : String(from || "")).trim().toLowerCase();
  var at = addr.lastIndexOf("@");
  return at >= 0 ? addr.slice(at + 1) : addr;
}

/* ------------------------------------------------------------------ gmail: discovery (admin) */

// after/before: "YYYY/MM/DD" Gmail search dates; when given they replace the newer_than window.
function discoverGmail(months, max, after, before) {
  var window = (after || before) ? ((after ? "after:" + after + " " : "") + (before ? "before:" + before + " " : "")) : "newer_than:" + months + "m ";
  var queries = [
    window + "-category:social -category:forums (subject:(bill OR statement OR payment OR receipt OR invoice OR autopay OR \"auto pay\" OR \"payment confirmation\" OR due OR premium OR renewal OR \"amount due\") OR \"amount due\" OR \"payment received\" OR \"thank you for your payment\")"
  ];
  var bills = readAll("bills");
  var senders = Object.keys(bills).filter(function (k) { return k.indexOf("__") !== 0 && !bills[k].del && bills[k].sender; }).map(function (k) { return bills[k].sender; });
  if (senders.length) queries.push(window + "from:(" + senders.join(" OR ") + ")");

  var seen = {}, ids = [];
  queries.forEach(function (q) {
    if (ids.length >= max) return;
    gmailSearch(q, max - ids.length).forEach(function (id) { if (!seen[id]) { seen[id] = true; ids.push(id); } });
  });

  var groups = {};
  ids.forEach(function (id) {
    var meta = gmailMeta(id);
    var g = groups[meta.domain];
    if (!g) g = groups[meta.domain] = { domain: meta.domain, from: meta.from, count: 0, dates: [], amounts: [], subjects: [], ids: [] };
    g.count++;
    g.dates.push(meta.date);
    var amt = extractAmount(meta.subject + " " + meta.snippet, null);
    if (amt != null && g.amounts.length < 12) g.amounts.push(amt);
    if (g.subjects.indexOf(meta.subject) < 0 && g.subjects.length < 6) g.subjects.push(meta.subject);
    if (g.ids.length < 3) g.ids.push(id);
  });

  var list = Object.keys(groups).map(function (k) {
    var g = groups[k];
    g.dates.sort();
    var gaps = [];
    for (var i = 1; i < g.dates.length; i++) gaps.push(daysBetween(g.dates[i - 1], g.dates[i]));
    gaps.sort(function (a, b) { return a - b; });
    var med = gaps.length ? gaps[Math.floor(gaps.length / 2)] : null;
    return { domain: g.domain, from: g.from, count: g.count, first: g.dates[0], last: g.dates[g.dates.length - 1],
             medianGapDays: med, cadenceGuess: cadenceGuess(med), amounts: g.amounts, subjects: g.subjects, ids: g.ids };
  }).sort(function (a, b) { return b.count - a.count; }).slice(0, 80);

  return { ok: true, months: months, window: window.trim(), scanned: ids.length, truncated: ids.length >= max, senders: list };
}

function gmailDate(x) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s(x));
  return m ? m[1] + "/" + m[2] + "/" + m[3] : "";
}

function cadenceGuess(med) {
  if (med == null) return "single";
  if (med <= 10) return "weekly";
  if (med >= 20 && med <= 40) return "monthly";
  if (med >= 75 && med <= 110) return "quarterly";
  if (med >= 160 && med <= 200) return "semiannual";
  if (med >= 330 && med <= 400) return "annual";
  return "irregular";
}

function peekMessage(id) {
  if (!id) return { ok: false, error: "id required" };
  var m = gmailFull(id);
  return { ok: true, id: m.id, from: m.from, subject: m.subject, date: m.date, body: m.body.slice(0, 4000) };
}

/* ------------------------------------------------------------------ gmail: scan */

// Daily trigger entry point (no args) and admin/app refresh entry point (with opts).
function scanGmail(opts) {
  opts = opts || {};
  var days = opts.days || SCAN_DAYS, dry = !!opts.dry, now = new Date().toISOString();
  var bills = readAll("bills"), payments = readAll("payments");
  if (bills.__unreadable) return { ok: false, error: "unreadable bills rows: " + bills.__unreadable.join(", ") };
  if (payments.__unreadable) return { ok: false, error: "unreadable payments rows: " + payments.__unreadable.join(", ") };

  var targets = Object.keys(bills).filter(function (k) {
    var b = bills[k];
    return k.indexOf("__") !== 0 && !b.del && b.active && b.sender && (!opts.bill || k === opts.bill);
  });
  var added = [], updatedBills = [], unmatched = [], warnings = [], dup = 0, checked = 0, billsDirty = false;
  var window = opts.months ? "newer_than:" + opts.months + "m" : "newer_than:" + days + "d";

  targets.forEach(function (billId) {
    var bill = bills[billId];
    var ids = gmailSearch("from:(" + bill.sender + ") " + window, 60);
    ids.forEach(function (id) {
      var key = "e-" + id;
      if (payments[key]) { dup++; return; }          // includes tombstones: a deleted payment stays deleted
      checked++;
      var meta = gmailMeta(id);
      var kind = classify(bill, meta, warnings);
      if (!kind) { if (unmatched.length < 30) unmatched.push({ billId: billId, gmailId: id, date: meta.date, subject: meta.subject }); return; }
      var full = gmailFull(id);
      var text = meta.subject + "\n" + full.body;
      var amount = extractAmount(text, bill.amountRegex, warnings);
      if (kind === "PAYMENT") {
        payments[key] = { billId: billId, date: meta.date, amount: amount, source: "email", gmailId: id, subject: meta.subject.slice(0, 120), u: now };
        added.push({ id: key, billId: billId, date: meta.date, amount: amount, subject: meta.subject.slice(0, 80) });
      } else {
        var due = extractDueDate(text), changed = false;
        if (amount != null && amount !== bill.lastBillAmount) { bill.lastBillAmount = amount; changed = true; }
        if (due && due !== bill.lastBillDue) { bill.lastBillDue = due; changed = true; }
        if (changed) { bill.u = now; billsDirty = true; updatedBills.push({ id: billId, lastBillAmount: bill.lastBillAmount, lastBillDue: bill.lastBillDue }); }
      }
    });
  });

  if (!dry) {
    var lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      if (added.length) writeAll("payments", payments);
      if (billsDirty) writeAll("bills", bills);
      setProp("LAST_SCAN", now);
    } finally { lock.releaseLock(); }
  }
  return { ok: true, dry: dry, window: window, bills: targets.length, checked: checked, added: added,
           updatedBills: updatedBills, skipped: { dup: dup }, unmatched: unmatched, warnings: warnings, lastScan: dry ? (getProp("LAST_SCAN") || "") : now };
}

// Used by the app's Refresh button: scan at most once every five minutes.
function scanIfDue() {
  var last = getProp("LAST_SCAN");
  if (last && (Date.now() - new Date(last).getTime()) < SCAN_MIN_GAP_MS) return { skipped: true, lastScan: last };
  var r = scanGmail({ days: SCAN_DAYS });
  if (!r.ok) return { error: r.error };
  return { checked: r.checked, added: r.added.length, updatedBills: r.updatedBills.length, lastScan: r.lastScan };
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) { if (t.getHandlerFunction() === "scanGmail") ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger("scanGmail").timeBased().everyDays(1).atHour(6).create();
  Logger.log("Daily scanGmail trigger installed (06:00 " + tz() + ").");
}

/* ------------------------------------------------------------------ debugging */

// Run from the editor; writes nothing. Shows what the script reads from each tab.
function debugRead() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  Logger.log("spreadsheet: " + ss.getName() + " | tabs: " + ss.getSheets().map(function (sh) { return sh.getName() + "(" + sh.getLastRow() + " rows)"; }).join(", "));
  NAMES.forEach(function (name) {
    var obj = readAll(name);
    var ids = Object.keys(obj).filter(function (k) { return k.indexOf("__") !== 0; });
    Logger.log(name + ": " + ids.length + " rows: " + ids.join(", "));
    if (obj.__unreadable) Logger.log(name + " UNREADABLE: " + obj.__unreadable.join(", "));
  });
  Logger.log("ADMIN_TOKEN set: " + !!getProp("ADMIN_TOKEN") + " | LAST_SCAN: " + (getProp("LAST_SCAN") || "never"));
}

/* ------------------------------------------------------------------ helpers */

function getProp(k) { return PropertiesService.getScriptProperties().getProperty(k); }
function setProp(k, v) { PropertiesService.getScriptProperties().setProperty(k, v); }
function tz() { return SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone(); }
function s(x) { return x == null ? "" : String(x).trim(); }
function pad2(n) { return (n < 10 ? "0" : "") + n; }
function numStr(n) { return n == null || isNaN(n) ? "" : String(n); }
function toNum(x) {
  if (x == null || x === "") return null;
  var n = typeof x === "number" ? x : Number(String(x).replace(/[$,\s]/g, ""));
  return isNaN(n) ? null : n;
}
function toBool(x) { return x === true || s(x).toUpperCase() === "TRUE" || s(x).toLowerCase() === "yes"; }
function clampInt(x, lo, hi, dflt) {
  var n = parseInt(x, 10);
  if (isNaN(n)) return dflt;
  return Math.max(lo, Math.min(hi, n));
}
function monthsList(x) {
  return uniq(s(x).split(/[,\s]+/).map(function (t) { return parseInt(t, 10); }).filter(function (n) { return n >= 1 && n <= 12; })).sort(function (a, b) { return a - b; });
}
function slug(name) {
  return s(name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "bill";
}
function uniqueSlug(name, taken) {
  var base = slug(name), id = base, n = 2;
  while (taken[id]) { id = base + "-" + n; n++; }
  return id;
}
function daysBetween(a, b) { return Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / 86400000); }
function isDate(x) { return typeof x === "string" && /^\d{4}-\d{2}-\d{2}$/.test(x); }
// Date cells from getValues() are not reliably `instanceof Date` in Apps Script, so duck-type them.
function isDateObj(x) { return !!x && typeof x === "object" && typeof x.getTime === "function" && !isNaN(x.getTime()); }
// A date cell is midnight in the spreadsheet's time zone; format it there to get back the typed date.
function dateStr(x) {
  if (isDateObj(x)) return Utilities.formatDate(x, tz(), "yyyy-MM-dd");
  return s(x);
}
function isoOf(x) {
  if (isDateObj(x)) return x.toISOString();
  var str = s(x);
  return /^\d{4}-\d{2}-\d{2}T/.test(str) ? str : "";
}
function uniq(a) { var seen = {}; return a.filter(function (x) { if (seen[x]) return false; seen[x] = true; return true; }); }
