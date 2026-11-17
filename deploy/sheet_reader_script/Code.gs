/**
 * Atlas -> one Google Sheet, read only. A web app the agent posts to; it hands back rows and nothing else.
 * One copy of this script per sheet the agent may read (2026-11-16: the work-block form's responses, and others).
 *
 * This script must be BOUND to the sheet (opened from the sheet: Extensions -> Apps Script). That is what lets it
 * run with the scope spreadsheets.currentonly: this one spreadsheet, no other file in your Drive. Google has no
 * scope that is both one-file and read-only, so the read-only part is this code: it has no line that writes, and
 * the agent can only call it, not change it.
 *
 * Setup (once per sheet):
 *  1. Open the sheet -> Extensions -> Apps Script. Paste this file over Code.gs.
 *  2. Project Settings (gear) -> tick "Show appsscript.json manifest file", then paste appsscript.json (alongside
 *     this file) over the one in the editor.
 *  3. Project Settings -> Script Properties -> add SHEET_TOKEN = the value of READ_SHEETS_TOKEN in the agent's
 *     data/.env (the same value for every sheet).
 *  4. Deploy -> New deployment -> Web app. Execute as: Me. Who has access: Anyone. Authorise when asked.
 *     Add "<a short name>=<the /exec URL>" to READ_SHEETS in data/.env (comma-separated).
 *  5. After editing this script: Deploy -> Manage deployments -> Edit -> New version.
 *
 * Request: POST, JSON body {token, action, ...}.
 *   tabs                          -> each tab's name, number of data rows and column headers
 *   rows {tab?, last?, offset?}   -> the last `last` data rows (default 20, at most 1000) of a tab (default: the
 *                                    first), skipping `offset` rows from the end; values as the sheet shows them
 */
var P = PropertiesService.getScriptProperties();

function json(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function doPost(e) {
  var p;
  try { p = JSON.parse(e.postData.contents); } catch (err) { return json({error: 'body is not JSON'}); }
  var want = P.getProperty('SHEET_TOKEN');
  if (!want || p.token !== want) return json({error: 'bad token'});
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (p.action === 'tabs') return json({title: ss.getName(), tabs: ss.getSheets().map(function (sh) {
      return {name: sh.getName(), rows: Math.max(0, sh.getLastRow() - 1), headers: headers(sh)}; })});
    if (p.action === 'rows') return json(rows(ss, p));
    return json({error: 'unknown action ' + p.action});
  } catch (err) { return json({error: String(err && err.message || err)}); }
}

function headers(sh) {
  return sh.getLastColumn() ? sh.getRange(1, 1, 1, sh.getLastColumn()).getDisplayValues()[0] : [];
}

function rows(ss, p) {
  var sh = p.tab ? ss.getSheetByName(p.tab) : ss.getSheets()[0];
  if (!sh) throw new Error('no tab named "' + p.tab + '"');
  var total = Math.max(0, sh.getLastRow() - 1);
  var last = Math.max(1, Math.min(parseInt(p.last, 10) || 20, 1000)), offset = Math.max(0, parseInt(p.offset, 10) || 0);
  var end = total - offset, start = Math.max(0, end - last);          // data rows [start, end), counted from 0
  var out = end > start ? sh.getRange(start + 2, 1, end - start, sh.getLastColumn()).getDisplayValues() : [];
  return {title: ss.getName(), tab: sh.getName(), total_rows: total, first_row: start + 1, headers: headers(sh), rows: out};
}
