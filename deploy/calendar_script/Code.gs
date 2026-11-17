/**
 * Atlas -> Google Calendar, write access. A web app the agent posts to; it runs as you, so no OAuth client or
 * Google Cloud project is needed. The agent still READS through the iCal feeds; this is only for changes.
 *
 * Permission: the one narrowest Google scope that allows this, calendar.events.owned ("See, create, change, and
 * delete events on Google calendars you own"). Not calendars you only subscribe to, not sharing, not settings,
 * not your email address. It is set in appsscript.json; the script uses the Calendar API (the "Calendar" advanced
 * service) because the simpler CalendarApp only works with full calendar access.
 *
 * Setup (once):
 *  1. https://script.google.com -> New project. Paste this file over Code.gs.
 *  2. Project Settings (gear) -> tick "Show appsscript.json manifest file", then paste appsscript.json (alongside
 *     this file) over the one in the editor.
 *  3. Project Settings -> Script Properties -> add CALENDAR_TOKEN = the value of CALENDAR_WRITE_TOKEN in the
 *     agent's data/.env   (copy it with: grep '^CALENDAR_WRITE_TOKEN=' data/.env | cut -d= -f2 | pbcopy)
 *  4. Deploy -> New deployment -> Web app. Execute as: Me. Who has access: Anyone. Authorise when asked.
 *     (The token check is what stops strangers.) Put the /exec URL in data/.env as CALENDAR_WRITE_URL.
 *  5. After editing this script: Deploy -> Manage deployments -> Edit -> New version.
 *
 * Rules built in here, not only in the agent:
 *  - It never adds guests and never asks Google to send updates, so it never sends an invitation or a notice.
 *  - It refuses to change or delete an event that has guests other than you (the event is partly theirs).
 *
 * Request: POST, JSON body {token, action, ...}. Times are ISO with an offset ("2026-11-20T15:00:00-08:00");
 * all-day events use dates ("2026-11-20"; end_date is the last day, inclusive).
 *   create  {title, start, end | date [, end_date], calendar_id?, location?, description?}
 *   update  {event_id, day_start, day_end, calendar_id?, title?, start?, end?, location?, description?}
 *   delete  {event_id, day_start, day_end, calendar_id?}
 * calendar_id: a calendar's id (its address, as in its iCal feed URL); left out, your main calendar. event_id is
 * the iCal id the feeds give. day_start/day_end bound the day the event is on now: for a repeating event they pick
 * the one occurrence that is changed or deleted; the rest of the series stays.
 */
var P = PropertiesService.getScriptProperties();
var QUIET = {sendUpdates: 'none'};

function json(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function doPost(e) {
  var p;
  try { p = JSON.parse(e.postData.contents); } catch (err) { return json({error: 'body is not JSON'}); }
  var want = P.getProperty('CALENDAR_TOKEN');
  if (!want || p.token !== want) return json({error: 'bad token'});
  try {
    if (p.action === 'ping') return json({ok: true});
    if (p.action === 'create') return json(create(p));
    if (p.action === 'update') return json(update(p));
    if (p.action === 'delete') return json(remove(p));
    return json({error: 'unknown action ' + p.action});
  } catch (err) { return json({error: String(err && err.message || err)}); }
}

function cal(p) { return p.calendar_id || 'primary'; }
function isDay(s) { return /^\d{4}-\d{2}-\d{2}$/.test(s || ''); }
function when(s) { if (!s || isNaN(new Date(s))) throw new Error('time must be ISO with an offset, got ' + s); return s; }
function dayAfter(s) { var d = new Date(s + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + 1); return d.toISOString().slice(0, 10); }
function dayBefore(s) { var d = new Date(s + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() - 1); return d.toISOString().slice(0, 10); }

function describe(ev) {
  var allDay = !!(ev.start && ev.start.date);
  return {event_id: ev.iCalUID, title: ev.summary || '', all_day: allDay,
          start: allDay ? ev.start.date : ev.start.dateTime, end: allDay ? dayBefore(ev.end.date) : ev.end.dateTime,
          location: ev.location || '', repeating: !!(ev.recurringEventId || ev.recurrence), link: ev.htmlLink || ''};
}

function create(p) {
  if (!p.title) throw new Error('title is required');
  var body = {summary: p.title, location: p.location || '', description: p.description || ''};
  if (p.date) {
    if (!isDay(p.date) || (p.end_date && !isDay(p.end_date))) throw new Error('date and end_date must be YYYY-MM-DD');
    body.start = {date: p.date};
    body.end = {date: dayAfter(p.end_date || p.date)};          // the API's end date is the day after the last day
  } else {
    if (new Date(when(p.end)) <= new Date(when(p.start))) throw new Error('end must be after start');
    body.start = {dateTime: p.start};
    body.end = {dateTime: p.end};
  }
  return {created: describe(Calendar.Events.insert(body, cal(p), QUIET))};
}

/** The occurrence of event_id inside [day_start, day_end) (for a one-off event, the event itself). */
function find(p) {
  if (!p.event_id) throw new Error('event_id is required');
  var hits = [], page;
  do {
    var r = Calendar.Events.list(cal(p), {timeMin: when(p.day_start), timeMax: when(p.day_end), singleEvents: true,
                                          maxResults: 250, pageToken: page});
    hits = hits.concat((r.items || []).filter(function (ev) { return ev.iCalUID === p.event_id; }));
    page = r.nextPageToken;
  } while (page && !hits.length);
  if (!hits.length) throw new Error('no event with that id on that day in this calendar (is it on another calendar or day?)');
  var ev = hits[0];
  var others = (ev.attendees || []).filter(function (a) { return !a.self; });
  if (others.length) throw new Error('this event has ' + others.length + ' other guest(s); change it by hand so they are not surprised');
  return ev;
}

function update(p) {
  var ev = find(p), was = describe(ev), body = {};
  if (p.title !== undefined) body.summary = p.title;
  if (p.location !== undefined) body.location = p.location;
  if (p.description !== undefined) body.description = p.description;
  if (p.start !== undefined || p.end !== undefined) {
    if (ev.start.date) throw new Error('this is an all-day event; delete it and create a timed one instead');
    var s = p.start !== undefined ? when(p.start) : ev.start.dateTime, e = p.end !== undefined ? when(p.end) : ev.end.dateTime;
    if (new Date(e) <= new Date(s)) throw new Error('end must be after start');
    body.start = {dateTime: s};
    body.end = {dateTime: e};
  }
  return {updated: describe(Calendar.Events.patch(body, cal(p), ev.id, QUIET)), was: was};
}

function remove(p) {
  var ev = find(p), was = describe(ev);
  Calendar.Events.remove(cal(p), ev.id, QUIET);
  return {deleted: was};   // Google keeps deleted events in the calendar's Trash for 30 days
}
