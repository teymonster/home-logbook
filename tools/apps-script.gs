/**
 * Home Logbook progress sync — Google Apps Script web app.
 *
 * Paste this into Extensions → Apps Script of the "Home Logbook progress"
 * Google Sheet. The Sheet needs one tab named "log" with the header row:
 *   id | last | history | updated | deleted
 *
 * Set a Script property TOKEN (Project Settings → Script properties) to a long
 * random string, then Deploy → New deployment → Web app → Execute as Me →
 * Who has access: Anyone. The app posts to the /exec URL with that token.
 *
 * Merge rule: per task, the record with the newest "updated" wins. A record
 * {del:true, updated} is a tombstone and wins when it is newer, so an undo that
 * empties a task propagates to other devices.
 *
 * After editing this file: Deploy → Manage deployments → pencil → Version: New.
 */

var SHEET = "log";
var HEADER = ["id", "last", "history", "updated", "deleted"];

function doGet(e) {
  var token = e && e.parameter && e.parameter.token;
  if (!authorized(token)) return out({ ok: false, error: "unauthorized" });
  return out({ ok: true, log: readAll() });
}

function doPost(e) {
  var body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out({ ok: false, error: "bad json" }); }
  if (!authorized(body.token)) return out({ ok: false, error: "unauthorized" });
  var incoming = body.log && typeof body.log === "object" ? body.log : {};
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var current = readAll();
    // Safety: never rewrite the tab from a partial read. If any row with an id could not be
    // parsed, refuse to save rather than drop it.
    if (current.__unreadable) {
      return out({ ok: false, error: "unreadable rows: " + current.__unreadable.join(", ") });
    }
    var merged = merge(current, incoming);
    writeAll(merged);
    return out({ ok: true, log: merged });
  } finally {
    lock.releaseLock();
  }
}

function authorized(token) {
  var want = PropertiesService.getScriptProperties().getProperty("TOKEN");
  return !!want && typeof token === "string" && token === want;
}

function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function sheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET);
  if (!sh) { sh = ss.insertSheet(SHEET); }
  if (sh.getLastRow() === 0) { sh.appendRow(HEADER); }
  return sh;
}

function readAll() {
  var sh = sheet();
  var last = sh.getLastRow();
  var log = {};
  if (last < 2) return log;
  var rows = sh.getRange(2, 1, last - 1, HEADER.length).getValues();
  var unreadable = [];
  rows.forEach(function (r) {
    var id = String(r[0] || "").trim();
    if (!id) return;
    var updated = isoOf(r[3]);
    if (String(r[4]).toUpperCase() === "TRUE") {
      log[id] = { del: true, u: updated };
      return;
    }
    // Column C may hold one date that Sheets converted to a date cell, or a comma list of text dates.
    var hist = isDateObj(r[2]) ? [dateStr(r[2])]
      : String(r[2] || "").split(",").map(function (s) { return dateStr(s); }).filter(isDate);
    var lastDate = isDate(dateStr(r[1])) ? dateStr(r[1]) : (hist.length ? hist[hist.length - 1] : "");
    if (!lastDate) { unreadable.push(id); return; }
    if (hist.indexOf(lastDate) < 0) hist.push(lastDate);
    hist = uniq(hist).sort();
    log[id] = { last: lastDate, history: hist.slice(-12), u: updated || (lastDate + "T00:00:00Z") };
  });
  if (unreadable.length) log.__unreadable = unreadable;
  return log;
}

function writeAll(log) {
  var sh = sheet();
  var ids = Object.keys(log).filter(function (k) { return k.indexOf("__") !== 0; }).sort();
  var rows = ids.map(function (id) {
    var v = log[id];
    if (v.del) return [id, "", "", v.u || "", "TRUE"];
    return [id, v.last, (v.history || [v.last]).join(","), v.u || "", ""];
  });
  var lastRow = sh.getLastRow();
  if (lastRow > 1) sh.getRange(2, 1, lastRow - 1, HEADER.length).clearContent();
  if (rows.length) {
    var range = sh.getRange(2, 1, rows.length, HEADER.length);
    range.setNumberFormat("@");   // plain text: stop Sheets turning dates into date cells
    range.setValues(rows);
  }
}

function merge(current, incoming) {
  var merged = {};
  var id;
  for (id in current) merged[id] = current[id];
  for (id in incoming) {
    var v = normalize(incoming[id]);
    if (!v) continue;
    var cur = merged[id];
    if (!cur || stamp(v) > stamp(cur)) merged[id] = v;
  }
  return merged;
}

function normalize(v) {
  if (!v || typeof v !== "object") return null;
  if (v.del) return { del: true, u: isoOf(v.u) || new Date().toISOString() };
  if (!isDate(v.last)) return null;
  var hist = (Array.isArray(v.history) ? v.history : [v.last]).filter(isDate);
  if (hist.indexOf(v.last) < 0) hist.push(v.last);
  hist = uniq(hist).sort().slice(-12);
  return { last: hist[hist.length - 1], history: hist, u: isoOf(v.u) || (v.last + "T00:00:00Z") };
}

function stamp(v) { return v && v.u ? String(v.u) : ""; }

function isDate(s) { return typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s); }

// Date cells from getValues() are not reliably `instanceof Date` in Apps Script, so duck-type them.
function isDateObj(x) { return !!x && typeof x === "object" && typeof x.getTime === "function" && !isNaN(x.getTime()); }

// A date cell is midnight in the spreadsheet's time zone; format it there to get back the typed date.
function dateStr(x) {
  if (isDateObj(x)) return Utilities.formatDate(x, SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone(), "yyyy-MM-dd");
  return String(x || "").trim();
}

function isoOf(x) {
  if (isDateObj(x)) return x.toISOString();
  var s = String(x || "").trim();
  return /^\d{4}-\d{2}-\d{2}T/.test(s) ? s : "";
}

function uniq(a) { var seen = {}; return a.filter(function (x) { if (seen[x]) return false; seen[x] = true; return true; }); }
