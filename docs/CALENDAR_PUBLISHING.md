# Personal calendars in NeuroShift

Open the usual `01NeuroShift.lnk`, then click **יומנים אישיים** in the main
window's header. Restart an already-open app after a code update.

## User workflow

1. Save the finalized Excel workbook after manual changes.
2. In **יומנים אישיים**, choose that workbook and click **תצוגה מקדימה**.
3. Review the detected month, names, roles and start/end dates. Filter by worker
   or show changes only. Added, removed and changed-hour events are distinguished.
4. Click **פרסום היומנים** to publish that exact preview. A changed source file
   or a newer publication requires a fresh preview. Repeating an unchanged
   publication does not deploy a new version or create duplicates.
5. In **יומנים וקישורים**, select a worker and copy their permanent subscription
   link. The calendars, links, private source Sheet and script project are
   visible here even before choosing a workbook. Nothing is sent to workers.

On 2026-11-02 the user authorized sharing the calendars with workers. Version 4
removes בדיקה/TEST from calendar names and event titles at the same deployment
and all 18 existing personal URLs. All 94 duty identities and start/end times are
unchanged. The serving state now has `test_mode=false`; future publishes retain
normal labels. Free/busy transparency is preserved. Google Calendar's initial
subscription was confirmed by the user; automatic-refresh timing and Apple/
Outlook behavior still need client verification. Page refresh does not force
Google to retrieve an external calendar immediately.

Version 6 uses the user's exact short name format, for example
`Neuroshift - שמואל` and `Neuroshift - פרץ`, in both standard `NAME` and
legacy `X-WR-CALNAME` fields, plus calendar-level descriptions. All 18 URLs and
94 duty identities are preserved. Friday כונן מיון and כונן שבץ use 13:00-09:00
the following day; full holidays retain the 09:00-08:00 override. The four
November כונן מיון Friday events were updated, with all other hours preserved.
Names deliberately omit the month so the permanent subscription spans months.
The links tab shows this name and provides **העתקת שם היומן** as a fallback.

Google was reported to display the URL despite the earlier X-WR-CALNAME field.
A fresh Google subscription using the additional standard NAME field has not yet
been confirmed. Do not promise that the source can override Google's saved/local
calendar label. Existing subscriptions can need a manual rename in their settings.
The response handler attempts a named .ics download with a safe fallback; the
hosted response inspected did not expose a Content-Disposition header, so do not
claim filename-based automatic naming has been verified.

Naming references: [RFC 7986 NAME](https://www.rfc-editor.org/rfc/rfc7986.html#section-5.1),
[Google's user-specific calendar title](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList),
[Apps Script named downloads](https://developers.google.com/apps-script/reference/content/text-output#downloadAsFile(String)).

## Implementation and guarantees

- `gui/calendar_window.py`: independent child window, queue-based background
  preview/publish, filters, previous-time details, visible links and copy action.
- `core/personal_calendars.py`: read-only XLSX/XLSM extraction, month validation,
  canonical holiday classification, confirmed duty hours, change comparison,
  stable event identities and UTF-8 iCalendar output.
- `core/calendar_publisher.py`: persistent monthly snapshots, account/resource
  verification, private Google Sheet upload/readback, explicit Apps Script
  publication, local publication state, anonymous feed checks and process lock.
- The workbook is read once into memory and never rewritten or recalculated.
  Required duty headers are recognized by name rather than column position;
  כ.שבץ is optional. Blank duties are shown as warnings and produce no event.
  Every date in the selected month must appear exactly once.
- Preview reads only the live `חגים` table using the scheduler's existing bot
  with read-only scope. Existing holiday helpers handle full holidays, explicit
  and inferred eves, informational dates and `חופש ללא ערב חג`. No scheduling
  engine, availability table, capability optimization or roster generation runs.
- Publishing replaces the selected month's events, preserving other published
  months. Removed workers keep valid empty feeds when no duties remain. Worker
  links and unchanged-duty UIDs persist; changed times advance event revisions.
- State and secret URLs stay in ignored `.calendar-pilot/state.json`. Published
  data is stored in `calendar_months`/schema 2. The old November-only CLI refuses to run,
  preventing it from overwriting the GUI's multi-month snapshots.
- Publishing uses the already saved Apps Script account connection in the user's
  `.clasprc.json`, refreshed in memory only. It verifies
  `shamir.neuroshift@gmail.com` and the owner-only pilot Sheet on every changed
  publication. No credential contents or full personal URLs are logged. A missing
  or revoked connection stops publishing with an explanatory message.
- The private backend resources retain their historical internal TEST names;
  these are not calendar/event labels. The resource check still verifies the
  original owner-only Sheet and the same resource IDs. Do not rename resources
  without updating that check.
- The publisher uploads the complete monthly snapshot to the dedicated private
  Sheet through Drive CSV conversion, checks readback, then publishes an immutable
  Apps Script version at the same deployment URL. It needs no new API enablement.
  The serving path has no Google scopes and no live Sheet reads.
- Failure before deployment leaves the local published baseline unchanged; the
  private source Sheet may contain the prepared candidate for a safe retry. After
  successful deployment, local state records the live version before optional
  feed checks. A failed check is reported as published-but-not-verified, rather
  than incorrectly reporting that no publication occurred.

## Verification

155 project unit tests and 15 separate pilot tests passed without running a
roster. Tests cover real date cells, month rollover, holidays, per-worker feeds,
reassignment/cancellation, idempotency, stale previews, preserved months/links,
failure handling and GUI actions using a fake cloud backend. Main window, preview
and links tab were also visually checked in the native Windows app.

The new preview was exercised on the real finalized November workbook with a
read-only live holiday lookup: 94 duties, 18 workers, no warnings and no changes
from the existing pilot. Its unchanged publish path performed no Google writes
and did not change saved calendar state. The scheduler module was not imported.

The subsequent authorized label release published version 4 and anonymously
verified all 18 feeds. Full calendar-month snapshots, worker keys, URLs and the
deployment ID were compared before/after and remained identical. A private
pre-release state backup is in ignored `.calendar-pilot/before-label-removal.json`.

The unused **שיפור סידור קיים** option and all optimizer UI handlers/state were
removed from `gui/app.py` at the user's request. The separate legacy utility
module is not exposed by the app.

Cloud resource details and earlier client-test results: [CALENDAR_PILOT.md](CALENDAR_PILOT.md).
