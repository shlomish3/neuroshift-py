# Personal calendar pilot - 2026-11-02

## Current state

The user authorized an isolated Google Sheets + Apps Script pilot using the real
final November roster, then authorized removing test labels for sharing with
workers. They use Google Calendar only. The calendars are now visible
through the app's **יומנים אישיים** screen; see [CALENDAR_PUBLISHING.md](CALENDAR_PUBLISHING.md).
No roster generation, source-workbook edits or worker notifications were performed.

- Owner: `shamir.neuroshift@gmail.com`, verified through Google identity and
  owner-only permissions on the new source Sheet.
- [Private calendar Sheet](https://docs.google.com/spreadsheets/d/1VW_m4Os86AldMCgrK1pYgRAOQv7BxHBlyQPr5Ry_rIQ/edit)
- **קישורי יומנים** in that Sheet lists all 18 workers, their calendar names and
  copyable subscription URLs. The app maintains it after successful publishing;
  **עדכון גיליון הקישורים** also refreshes it without redeploying calendars.
- [Separate Apps Script project](https://script.google.com/home/projects/1eAN4dbKx3rk7Y9_lCLoySwesoLWwGL0UOUH6_vuR32tABWD5pHBmIBFG/edit)
- Deployed version 6 serves the same 18 secret personal calendar links, covering 94 actual
  duties. Test labels were removed from names/titles at the user's request. Event
  identities, duty times and free/busy transparency were preserved.
- Version 6 supplies the exact `Neuroshift - <worker>` names in NAME and X-WR-CALNAME,
  plus descriptions. All anonymous feeds passed byte-exact verification; a new
  Google subscription must still confirm whether it uses this name rather than
  the URL. The app provides a copy-name fallback for a locally retained URL label.
- The screenshot showed mixed names from different versions, including an old
  TEST name. Do not assume new feed metadata renames existing Google entries;
  their displayed names may need one manual rename using the app's copy button.
- Source: `../../סידורי עבודה/סידור נובמבר 2026/סידור נובמבר 2026.xlsm`,
  `תורנויות!B4:G33`. Read recognized headers in row 3 and direct final duty cells,
  not the generated audit sheet or availability/summary helpers. Blank כ.שבץ cells
  produce no calendar event. Current stroke duties: ברטל on November 2, 10, 18, 29.
- User shared the original workbook with NeuroShift during this chat. Drive
  metadata confirmed personal-account ownership, NeuroShift editor access, and
  matching cloud/local file checksum at the time of that check. Original Drive
  file ID: `1vlnUApVFnSKG6uJav-38PtHrsUNug8RO`.
- The workbook was externally resaved during verification. Every duty cell and
  source-cell reference still matched. The pilot never writes the workbook.

## Confirmed duty times

All times use `Asia/Jerusalem`, converted to UTC instants for the calendar file.
Both resident types use the same policy; `ת.מיון` is the workbook's first duty.

| Duty | Starts | Ends |
| --- | --- | --- |
| Resident, Sunday-Thursday | 16:00 | Next day 10:00 |
| Resident, Friday or ערב חג | 13:00 | Next day 10:00 |
| Resident, Saturday or full holiday marked חופש | 09:00 | Next day 08:00 |
| כונן מיון / כונן שבץ, ordinary Sunday-Thursday and weekday eves | 15:00 | Next day 08:00 |
| כונן מיון / כונן שבץ, Fridays | 13:00 | Next day 09:00 |
| כונן מיון / כונן שבץ, Saturday or full holiday marked חופש | 09:00 | Next day 08:00 |

The Saturday/full-holiday 09:00-08:00 correction and כ.שבץ support supersede the
initial timing rules. These are calendar-export hours only, not a scheduler change.
The later Friday on-call correction supersedes its original 15:00-08:00 rule.

November holiday data was read from the live `חגים` Sheet. The only November
entry is 09/11/2026 סיגד, סוג=מידע; it keeps normal weekday hours. The pilot is
locked to November. A production exporter must use the canonical holiday helpers,
including explicit/inferred eves and `חופש ללא ערב חג`, rather than freezing this
month's holiday snapshot.

## How the pilot works

The original `tools/calendar_pilot/pilot.py` is separate from the scheduler. Initial commands:
`prepare`, `cloud-create`, `sync-sheet`, `publish`, `verify`. No action runs on
import. Source-duty changes require explicit `prepare --accept-source-update`
after reviewing the differences. `sync-sheet` updates only the existing owner-only
pilot raw-data Sheet using Drive CSV conversion, then checks every readback row.
The Sheets API in the CLI OAuth project is disabled; it was not enabled.

The new app screen uses `core/calendar_publisher.py` and supports arbitrary months,
previews and persistent monthly snapshots. Use the app for subsequent publishing;
the initial CLI is November-only and refuses to run after the GUI's schema migration.

The app publisher now uses XLSX export/edit/import through Drive to retain the
links tab and other worksheet cells when updating its assignment table. It reads
the Sheet back and compares every record, then compiles the verified snapshot into a versioned
Apps Script deployment. The public serving path needs no Google OAuth scopes or
live Sheet reads. Sheet edits do not change feeds until an explicit publish.
Revisions preserve personal URLs and event UIDs and advance event sequence/time.
The original CLI's CSV replacement is historical; use the app's multi-tab path.

Personal keys, full URLs, extracted CSV/ICS files, source fingerprints and cloud
resource IDs are in ignored `.calendar-pilot/`. Do not commit them or distribute
the links broadly. The public endpoint requires a valid personal key and provides
no worker directory. Holders of a valid link can read that worker's duties.

## Verification and remaining work

- Focused offline checks cover final source extraction, per-worker filtering,
  Hebrew/UTF-8 folding, escaping, duty hours, DST, month end, stable event UIDs,
  revision sequences, and external workbook resaves versus changed duty cells.
- The actual Apps Script handler passed offline valid/invalid/missing/prototype
  key checks.
- All 18 deployed feeds were fetched anonymously after version 3: HTTP 200,
  `text/calendar`, correct Google redirects, and byte-exact expected content for
  all 94 duties. Missing/invalid keys returned no calendar data.
- Google Calendar: the user confirmed the initial URL subscription succeeded and
  all six שמואל duties appeared (November 3, 8, 12, 14, 25, 28). This is a
  user-reported client result, distinct from the automated HTTP checks.
- Latest revision: initial 90 assignments unchanged, four כ.שבץ assignments added,
  original personal URLs and original-duty UIDs retained. In the שמואל feed,
  November 14 and 28 now end at 08:00 the following day. Google refresh and absence
  of duplicate events after this revision still need client confirmation.
- About five minutes after the 18:06 Israel-time publication, the user still saw
  the previous hours after refreshing Google Calendar. A new anonymous fetch
  confirmed both Saturday events already end at 08:00 in the live feed. This is
  not yet evidence of a failed subscription; Google's refetch time remains unknown.
- Current Google URL-subscription documentation gives no guaranteed refresh
  interval. [Maven's integration guidance](https://help.maven.com/en/articles/6329843-subscribing-to-a-calendar-by-url)
  reports historical Google refresh of 18-24 hours, not a guarantee.
  [Sugar's integration guidance](https://support.sugarai.com/knowledge_base/calendar/troubleshooting_feed_refresh_in_external_calendars/)
  explains that Google Calendar's refresh control does not force retrieval of
  subscribed feeds. Do not present Sugar's separately quoted older 8-hour limit
  as a current Google guarantee.
- Apple Calendar and Outlook: no client test performed. Do not call them verified.
- Further calendar-client additions/corrections/removals, duplicate handling and refresh
  latency have not yet been tested. HTTP success is not client compatibility proof.
- Computer control stopped because the current Chrome URL could not be verified.
  No alternate browser-control method was used after that stop. Cloud preparation
  and anonymous checks continued through purpose-built Google APIs/HTTP requests.

The initial documentation research remains in [CALENDAR_FEASIBILITY.md](CALENDAR_FEASIBILITY.md).
