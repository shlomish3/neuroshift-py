# Personal calendar investigation - 2026-11-02

This records the initial research and read-only account checks. The user later
authorized a separate real-data pilot; current deployment details, confirmed hours
and test status are in [CALENDAR_PILOT.md](CALENDAR_PILOT.md). The account findings
below describe the initial checks, before the user shared the November workbook.

## Decision status

Google Sheets + Apps Script is technically feasible, but cross-client reliability
is not established. Vendor documentation confirms iCalendar serving and URL
subscriptions separately; it does not certify this exact combination. Do not
describe it as tested in Google Calendar, Apple Calendar, or Outlook.

- Google Calendar: From URL subscriptions are supported; initial setup requires a
  computer browser. No guaranteed refresh interval was established from the docs.
- Apple Calendar: subscription URLs are supported on iPhone/iPad/Mac. Actual feed
  retrieval, changes, and phone display still require testing.
- Outlook.com/Outlook on the web: web subscriptions are supported, but Microsoft
  warns refresh may take more than 24 hours. Windows client variants need separate
  verification; do not infer universal compatibility from the web documentation.
- Apps Script ContentService supports iCal and redirects responses to a one-time
  script.googleusercontent.com URL. Subscribe to the persistent /exec URL, never
  the temporary redirect target. Redirect handling by each client remains untested.
- Feed retrieval must work without interactive Google login, running as the
  deploying account with anonymous access. Each worker needs a secret, revocable
  link; the source Sheet stays private. Link holders can read that worker's feed.
- Apps Script has execution limits (currently 30 concurrent executions per user
  and 6 minutes per execution). Small team scale appears reasonable by inference,
  not a load test. Serve a consistent published snapshot; avoid exposing drafts
  or returning an empty calendar when storage retrieval fails.

## Google identities verified live

| Resource | Verified identity/access |
| --- | --- |
| Python scheduler | Uses svc.json: neuroshift-bot@neuroshift.iam.gserviceaccount.com, project neuroshift. Metadata-only Sheets reads succeeded for all four configured workbooks. |
| 01 משמרות-Template | Owner shlomi9994@gmail.com; shamir.neuroshift@gmail.com and the scheduler bot are editors. |
| 02 תגובות לתורנויות וחופשים נוירולוגיה | Owner shlomi9994@gmail.com; shamir.neuroshift@gmail.com is an editor. Also has an anyone/reader permission. Not changed. |
| 03 יומן מרפאות and 04 Neuro Shift History | Bot can read them through Sheets. The verified NeuroShift CLI account received Drive file-not-found responses despite its drive.metadata.readonly scope; their human ownership remains unverified. |
| Saved Apps Script CLI login | Refreshed successfully in memory; Google confirmed shamir.neuroshift@gmail.com. No credential files were rewritten. |
| Availability Apps Script | Bound to the response workbook above. Last modifying user is shamir.neuroshift@gmail.com. Latest numbered deployment inspected: version 182, anonymous access, execute as deployer. Deployer identity itself was not returned by this metadata. |
| Legacy Shift macro | Bound to the settings workbook above; project metadata accessible to the NeuroShift CLI login. This does not mean the Python app uses that legacy macro. |
| Separate clinic-upload automation | Saved user token could not be refreshed. Its human account could not be identified; do not assume it is either Google account. OAuth client project is neuroshift. |
| Cloud administration | Bot project's Drive API is disabled. NeuroShift CLI could not inspect enabled services on the bot project. Cloud project ownership/admin roles were not established. No APIs were enabled. |

The desktop app's local Drive folder does not determine the remote execution
identity. Current setup mixes personal ownership, NeuroShift editing/development,
and bot access. The NeuroShift account is in active use, but ownership has not
been consolidated into it.

For a future pilot, prefer a separate calendar-only Sheet and standalone script
owned by shamir.neuroshift@gmail.com. Do not migrate or modify working scheduling
resources as part of that pilot. Serving ICS does not itself require Calendar API
access or access to workers' personal calendars.

## Proposed validation gate - not performed

1. If later authorized, publish an isolated synthetic feed with no real staff
   information. Confirm the stable URL returns calendar content anonymously,
   follows redirects successfully, and never requires cookies or a Google login.
2. Subscribe using Google Calendar web plus its phone display, Apple Calendar on
   iPhone (Mac if needed), and Outlook web plus the Windows version actually used.
3. Verify Hebrew, Israel timezone/DST, overnight end dates, empty-duty months,
   month rollover, and multiple resident/senior duty types.
4. Add, edit, move, remove, and reassign synthetic duties; republish unchanged
   data twice. Verify both workers' feeds, no duplicates, and measured refresh
   delays over at least 48 hours (longer if clients have not refreshed).
5. Verify unknown/revoked links reveal no duties and publishing failures preserve
   the last complete snapshot. Test updating the deployment without changing the
   subscription address.
6. Approve the delivery method only after those checks. Keep direct messages for
   urgent changes; subscription polling cannot guarantee immediate notification.

## Primary sources

- Apps Script iCal and redirects: https://developers.google.com/apps-script/guides/content
- Deployment identity/access: https://developers.google.com/apps-script/guides/web
- Execution limits: https://developers.google.com/apps-script/guides/services/quotas
- Google Calendar URL subscriptions: https://support.google.com/calendar/answer/37100?hl=en
- Apple subscriptions: https://support.apple.com/en-la/102301
- Outlook subscriptions and delay: https://support.microsoft.com/en-us/outlook/import-or-subscribe-to-a-calendar-in-outlook-com-or-outlook-on-the-web

Account evidence came from selected local credential metadata, authenticated
Google identity checks, Drive ownership/permission metadata, Sheets title-only
reads, and Apps Script project/deployment metadata. Secrets and tokens are not
included here. Browser inspection timed out; no browser account changes occurred.
