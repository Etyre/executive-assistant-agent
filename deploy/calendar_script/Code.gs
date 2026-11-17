/**
 * Atlas -> Google Calendar, write access. A web app the agent posts to; it runs as you, so no OAuth client or
 * Google Cloud project is needed. The agent still READS through the iCal feeds; this is only for changes.
 *
 * Setup (once):
 *  1. https://script.google.com -> New project. Paste this file over Code.gs.
 *  2. Project Settings (gear) -> tick "Show appsscript.json manifest file", then paste appsscript.json (alongside
 *     this file) over the one in the editor. It limits the script to Calendar.
 *  3. Project Settings -> Script Properties -> add CALENDAR_TOKEN = the value of CALENDAR_WRITE_TOKEN in the
 *     agent's data/.env   (copy it with: grep '^CALENDAR_WRITE_TOKEN=' data/.env | cut -d= -f2 | pbcopy)
 *  4. Deploy -> New deployment -> Web app. Execute as: Me. Who has access: Anyone. Authorise when asked.
 *     (The token check is what stops strangers.) Put the /exec URL in data/.env as CALENDAR_WRITE_URL.
 *  5. After editing this script: Deploy -> Manage deployments -> Edit -> New version.
 *
 * Rules built in here, not only in the agent:
 *  - It never adds guests, so it never sends an invitation.
 *  - It refuses to change or delete an event that has guests other than you (they could be notified, and the
 *    event is partly theirs). Do those by hand.
 *  - It only touches calendars you own.
 *
 * Request: POST, JSON body {token, action, ...}. Times are ISO with an offset ("2026-11-20T15:00:00-08:00");
 * all-day events use dates ("2026-11-20"; end_date is the last day, inclusive).
 *   calendars                                                  -> the calendars you own
 *   create  {title, start, end | date [, end_date], calendar?, location?, description?}
 *   update  {event_id, date, calendar?, title?, start?, end?, location?, description?}   (date: the day it is on now)
 *   delete  {event_id, date, calendar?}
 * `calendar` is a calendar's name; left out, your default calendar. For a repeating event, `date` picks the one
 * occurrence that is changed or deleted; the rest of the series stays.
 */
var P = PropertiesService.getScriptProperties();

function json(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function doPost(e) {
  var p;
  try { p = JSON.parse(e.postData.contents); } catch (err) { return json({error: 'body is not JSON'}); }
  var want = P.getProperty('CALENDAR_TOKEN');
  if (!want || p.token !== want) return json({error: 'bad token'});
  try {
    if (p.action === 'calendars') return json({calendars: CalendarApp.getAllOwnedCalendars().map(function (c) {
      return {name: c.getName(), default: c.isMyPrimaryCalendar(), timezone: c.getTimeZone()}; })});
    if (p.action === 'create') return json(create(p));
    if (p.action === 'update') return json(update(p));
    if (p.action === 'delete') return json(remove(p));
    return json({error: 'unknown action ' + p.action});
  } catch (err) { return json({error: String(err && err.message || err)}); }
}

function calendarFor(p) {
  if (!p.calendar) return CalendarApp.getDefaultCalendar();
  var owned = CalendarApp.getAllOwnedCalendars().filter(function (c) { return c.getName() === p.calendar; });
  if (!owned.length) throw new Error('no calendar you own is named "' + p.calendar + '"');
  return owned[0];
}

function day(s) { var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || ''); if (!m) throw new Error('date must be YYYY-MM-DD, got ' + s); return new Date(+m[1], +m[2] - 1, +m[3]); }
function when(s) { var d = new Date(s); if (!s || isNaN(d)) throw new Error('time must be ISO with an offset, got ' + s); return d; }
function nextDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1); }

function describe(ev, cal) {
  return {event_id: ev.getId(), calendar: cal.getName(), title: ev.getTitle(), all_day: ev.isAllDayEvent(),
          start: ev.isAllDayEvent() ? Utilities.formatDate(ev.getAllDayStartDate(), cal.getTimeZone(), 'yyyy-MM-dd') : ev.getStartTime().toISOString(),
          end: ev.isAllDayEvent() ? Utilities.formatDate(new Date(ev.getAllDayEndDate().getTime() - 86400000), cal.getTimeZone(), 'yyyy-MM-dd') : ev.getEndTime().toISOString(),
          location: ev.getLocation(), repeating: ev.isRecurringEvent()};
}

function create(p) {
  if (!p.title) throw new Error('title is required');
  var cal = calendarFor(p), opts = {location: p.location || '', description: p.description || ''}, ev;
  if (p.date) {
    ev = p.end_date && p.end_date !== p.date ? cal.createAllDayEvent(p.title, day(p.date), nextDay(day(p.end_date)), opts)
                                             : cal.createAllDayEvent(p.title, day(p.date), opts);
  } else {
    var s = when(p.start), e = when(p.end);
    if (e <= s) throw new Error('end must be after start');
    ev = cal.createEvent(p.title, s, e, opts);
  }
  return {created: describe(ev, cal)};
}

/** The occurrence of event_id on `date` (for a one-off event, the event itself). */
function find(p) {
  if (!p.event_id) throw new Error('event_id is required');
  var cal = calendarFor(p), d = day(p.date);
  var hits = cal.getEvents(d, nextDay(d)).filter(function (ev) { return ev.getId() === p.event_id; });
  if (!hits.length) throw new Error('no event with that id on ' + p.date + ' in "' + cal.getName() + '" (is it on another calendar or day?)');
  var ev = hits[0], me = Session.getEffectiveUser().getEmail();
  var others = ev.getGuestList().filter(function (g) { return g.getEmail() !== me; });
  if (others.length) throw new Error('this event has ' + others.length + ' other guest(s); change it by hand so they are not surprised');
  return {cal: cal, ev: ev};
}

function update(p) {
  var f = find(p), ev = f.ev, was = describe(ev, f.cal);
  if (p.title !== undefined) ev.setTitle(p.title);
  if (p.location !== undefined) ev.setLocation(p.location);
  if (p.description !== undefined) ev.setDescription(p.description);
  if (p.start !== undefined || p.end !== undefined) {
    if (ev.isAllDayEvent()) throw new Error('this is an all-day event; delete it and create a timed one instead');
    var s = p.start !== undefined ? when(p.start) : ev.getStartTime(), e = p.end !== undefined ? when(p.end) : ev.getEndTime();
    if (e <= s) throw new Error('end must be after start');
    ev.setTime(s, e);
  }
  return {updated: describe(ev, f.cal), was: was};
}

function remove(p) {
  var f = find(p), was = describe(f.ev, f.cal);
  f.ev.deleteEvent();
  return {deleted: was};   // Google keeps deleted events in the calendar's Trash for 30 days
}
