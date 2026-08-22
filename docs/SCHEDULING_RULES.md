# NeuroShift Scheduling Rules and Project Memory

This document is the canonical collective memory for scheduling behavior agreed during the NeuroShift project chats. Future changes should improve the existing working scheduler incrementally, not replace it. When a later explicit user instruction conflicts with this document, the later instruction wins and this document should be updated.

## General Precedence

Use this order when rules compete:

1. Hard eligibility and safety constraints.
2. Fixed assignments (`שיבוצים קבועים`).
3. The protected resident-duty optimization order below.
4. Other balancing and pairing preferences.
5. Preferred/requested dates as seeding guidance.
6. History and recency as an exact tie-breaker only.

Hard constraints include capability, submitted days off, vacations, complete or night-duty unavailability, post-duty rest, resident adjacent-night restrictions, incompatible same-day work, and other genuine safety constraints.

## Fixed Assignments

- A fixed assignment must not be removed or replaced for balancing, preferred requests, history, pairing, or another soft objective.
- A fixed assignment may change only when keeping it would violate a genuine hard eligibility or safety constraint.
- A later fixed resident night blocks assignment of a non-fixed resident night to the same worker on the preceding date. If such a conflict is discovered during cleanup, remove/refill the non-fixed earlier night and preserve the fixed later night. Two conflicting fixed nights remain a genuine hard conflict unless an explicit dated exception applies.
- When a hard constraint defeats a fixed assignment, log the conflict and legally refill the resulting vacancy when possible.
- A fixed `כונן מיון` assignment remains the anchor for that date. Associated pairing rules may adapt around it, but optimization must not replace the fixed on-call senior.

Fixed assignments are therefore highly protected, but they are not equal to hard safety constraints: hard constraints win.

## Resident Night Duties

Resident night duties are `ת.מיון` and `ת.מיון 2`.

### One-Time Yom Kippur 2026 Exception

- For the consecutive nights `20/09/2026` and `21/09/2026` only, the same resident may perform both resident night duties when that resident is explicitly entered in `שיבוצים קבועים` on both dates.
- The exception is derived from the fixed assignments after availability filtering. It does not authorize the algorithm to create a consecutive pair for another resident.
- It relaxes only the adjacent-resident-night and next-day-rest conflict for the second resident night itself. Capability, vacation, unavailability, next-day clinic, same-day compatibility, and all other hard rules remain active.
- Morning/day work on `21/09/2026` is still removed after the `20/09/2026` night, and ordinary post-duty rest still applies on `22/09/2026` after the `21/09/2026` night.
- Both duties count normally in totals, weekend/Saturday balance, history, and duty-type balance, and remain protected as fixed assignments.
- No other date pair, including another pair in September or any later month, inherits this exception automatically.

### Holiday and Holiday-Eve Classification

- A full holiday/rest day is a `חגים` row whose `סוג` contains `חופש`, except when the holiday name itself begins with `ערב `.
- A holiday name beginning with `ערב ` is always treated as `ערב חג` (Friday-equivalent), including the normal live-sheet form `סוג=מידע`. This protects the roster from accidentally treating an eve as a full holiday when its `סוג` was entered as `חופש`.
- The day before a genuine full holiday may still be inferred as Friday-equivalent when an explicit eve row is absent. A misclassified row whose name begins with `ערב ` is excluded from that inference source.

### Protected Optimization Order

Preserve this exact lexicographic order:

1. Fill missing resident duties, without violating hard rules.
2. Total-duty fairness.
3. Balance weekends and Fridays.
4. Balance Saturdays.
5. Minimize the total number of sandwiches.
6. Distribute unavoidable sandwiches fairly.
7. Balance `ת.מיון` versus `ת.מיון 2` as much as possible.

A lower item may improve only while every higher item remains equal. Do not reorder these priorities without explicit approval.

### Total-Duty Fairness

- Among active, legally participating residents, the final total-duty spread should be no greater than 1 whenever legally feasible.
- If a spread greater than 1 is genuinely unavoidable, keep all hard rules and emit a useful diagnostic instead of silently accepting the result or violating safety.
- A worker who cannot participate in the month must not distort the fairness pool.
- Fairness and history comparisons use the flexible resident pool: residents with at least one movable resident duty or legal capacity to receive one. A resident whose duties are all protected and who cannot legally receive a movable duty remains assigned but does not distort the flexible comparison target.

### Sandwich Definition

A sandwich is an alternating-night pair for the same resident on dates `D` and `D+2`, with no resident night duty on `D+1`. First minimize the total number of such pairs; only then balance unavoidable pairs between residents. When the resident explicitly requested both endpoint dates and is ultimately assigned both, that pair is an acceptable requested sandwich: allow it during preferred-date seeding/recovery and exclude it from sandwich-total, sandwich-distribution, repair, and rolling-sandwich penalties. A sandwich-only repair must not remove either requested endpoint, including by using that assignment as the other side of a swap intended to improve another resident's sandwich count. It is not protected from hard rules or higher objectives 1–4 (missing duties, total fairness, weekend/Friday balance, and Saturday balance). Requesting only one endpoint does not create this exception. Repairs must compare the net actionable-sandwich result: a replacement that creates one actionable sandwich may still be useful when it removes more actionable sandwiches overall, provided every higher protected priority remains safe. Existing replacement and two-person swap searches should consider multiple legal alternatives instead of stopping after the first protected-objective failure.

### Preferred Dates and History

- Preferred/requested resident-duty dates guide initial seeding.
- Preference preservation is enforced after every protected balancing stage,
  not only in a final cleanup pass. If the completed stage and every higher
  protected priority can remain exactly unchanged, restore a lost requested
  duty by moving a non-requested alternative instead. A later stage may still
  sacrifice the request only when its own protected improvement cannot
  otherwise be achieved legally.
- The same stage-local safeguard applies across missing-duty coverage,
  total-duty fairness, weekend/Friday balance, Saturday balance, sandwich
  total, sandwich distribution, and duty-type balance. At each point, lower
  stages that have not yet been optimized remain free to change.
- A starred preferred date is stored and projected as `DD/MM/YYYY יום X' (חשוב)`. It is parsed as stronger seeding guidance than a regular preferred date, but it is not a hard constraint or a protected optimization objective.
- Within the same request strength, prefer retaining Friday/Saturday requests over weekday requests whenever a protected balancing stage has multiple equally valid repairs. This is a request-level tie-breaker only: it must never prevent improvement of a higher protected priority.
- When two residents in the same request-strength/date class compete for the same non-fixed requested-duty capacity, first compare the projected result under the complete protected resident core. If that core is equal, prefer the resident with the lower percentage of their *other* preferred requests already fulfilled. A resident with no other requests is treated as 0% for this comparison.
- Recompute that percentage from the live trial roster after every seed, swap, and balancing repair. Do not freeze it from the initial request list or use a worker-level cached explanation. Equal request-class removals should preferentially come from the resident whose other-request approval percentage is higher.
- Across rosters with the same number and classes of fulfilled requests, prefer distributing approvals instead of repeatedly fulfilling requests for the same resident. This remains guidance outside the protected core: any genuine improvement in priorities 1–7 may still displace a request.
- A preferred date cannot override a hard rule or any protected optimization objective.
- Previous-month compensation and recency are consulted only when the entire protected current-month core and the preferred-request coverage/distribution result are equal.
- History must never worsen missing-duty coverage, current-month total fairness, weekend/Friday balance, Saturday balance, sandwich objectives, or duty-type balance.
- History must also not worsen preferred-request coverage or its approval-percentage distribution. After core equality, the request percentage tie-break therefore precedes history; deterministic jitter/name ordering is used only after both are equal.
- Within an exact protected-core tie, historical compensation considers rolling totals, rolling weekends, rolling sandwiches, and finally whether a previously burdened resident can receive the less burdensome `ת.מיון 2` side of an otherwise equally balanced odd duty split.
- If the history/recency tie-break displaces a seeded preferred date, record that exact stage and the alternate date received by the resident instead of emitting an untracked-removal diagnostic.

### Active and New Workers

- A worker with `0` capability for every shift in the `עובדים` sheet is unavailable for the entire month.
- Exclude such a worker from assignment and from fairness pools.
- `שיר` currently remains excluded because all her capabilities are `0`. Include her only after the user enables relevant capabilities and her availability/vacations have been submitted.

## Post-Night and Next-Morning Rules

- A resident must not remain assigned to ordinary morning work after a resident night duty.
- An actual next-day clinic blocks assignment of the preceding night duty. The clinic is scheduled first and wins this hard conflict.
- Non-clinic morning work such as `מיון`, `מחלקה`, `מחקר`, or ordinary rotation must not pre-emptively block the preceding night duty.
- When a night duty is assigned, incompatible non-clinic work on the following morning is removed, including a fixed morning assignment when necessary to enforce the hard post-duty rest rule.
- The resulting morning vacancy should be legally refilled when possible.

## Full-Month Rotation as a Reserve Pool

- A worker placed in a full-month fixed `רוטציה` remains protected whenever an ordinary legally eligible worker can cover the required daytime shift.
- `רוטציה` may act as a reserve pool only after the ordinary eligible candidate pool for that shift is exhausted. Improving the balance of removed rotation days must never make a rotation worker outrank an ordinary candidate.
- When more than one rotation worker is the only remaining legal reserve, balance the unavoidable rotation withdrawals between those workers.
- Re-check earlier reserve withdrawals after night-duty optimization and post-duty cleanup. If an ordinary worker has become legally available, replace the reserve worker in the daytime shift and restore the fixed `רוטציה` assignment.
- Genuine hard constraints such as post-duty rest, vacation, or universal unavailability may still remove a rotation assignment.

## Clinic Sources of Truth

A clinic assignment may originate only from:

- `שיבוצים קבועים`, or
- `יומן מרפאות` / the actual clinic calendar.

Legacy weekly clinic metadata such as `מרפאות קבועות` may support lookup or display behavior, but it must not independently create or force a worker's clinic assignment. In particular, it must not hard-assign a resident to a clinic that is absent from both approved sources.

## Friday Rules for Residents

- Prefer the Friday `ת.מיון` resident for Friday `מיון`.
- Prefer the Friday `ת.מיון 2` resident for Friday `מחלקה`.
- These are pairing preferences, not hard rules. Hard eligibility and the protected resident optimization core outrank them.
- Required staffing and row capacity must remain valid.

## Friday and Weekend Rules for Seniors

### Friday-Morning Balance

- Balance Friday-morning work among active seniors, ideally approximately one Friday per senior when legally feasible.
- Count a Friday once per senior even when that senior performs more than one Friday-morning role on that date.
- `כונן מיון` by itself is a night on-call duty and does not consume the senior's Friday-morning target.
- Friday-morning balance is stronger than the preference that Friday `כונן מיון` and `אטנדינג` be the same person.
- Therefore, use the same senior for Friday `כונן מיון` and `אטנדינג` only when doing so does not worsen the protected Friday-morning distribution.
- When a Friday `כונן מיון` comes from `שיבוצים קבועים`, keep that fixed senior as on-call and try to use the same person for `אטנדינג` only if Friday balance, hard eligibility, and row capacity allow it.

### Friday/Saturday On-Call Pair

- Friday and Saturday `כונן מיון` should be the same senior whenever legally possible.
- A fixed assignment on either day should anchor the pair rather than be replaced for balance.
- This paired senior weekend duty is a deliberate exception to an ordinary adjacent-night preference for senior on-call work. It does not relax resident adjacent-night safety rules.

### Shimon's Current Special Behavior

- `שמעון` performs `ייעוצים מובילים` only on Fridays.
- His current `כונן מיון` target is 2 per month.
- His Friday assignment uses previous-month history: generally avoid assigning him a Friday in consecutive months, and use at most one Friday-morning date in a month when he is due.
- These special rules remain in force until explicitly changed by the user.

## Assignment and Request Audit in `הסבר תורנויות`

Keep three compact, separate audit sections:

1. `הסבר שיבוצים בפועל`: one row for every final `ת.מיון`, `ת.מיון 2`, and `כונן מיון` assignment.
2. `תורנויות חסרות`: every unfilled duty and its legal alternatives or blocking reasons.
3. `בקשות מועדפות שלא שובצו`: every unfulfilled personal/preferred date request and a short causal explanation.

For completed assignments:

- Key every explanation to the exact `(date, shift, worker)` assignment. Never reuse a worker-level explanation across different dates.
- Explain why that specific assignment was retained: fixed assignment, mandatory personal rule, fulfilled preferred request, or the relevant balancing decision.
- Include concise date-specific context, such as the assignment's ordinal position within that worker's monthly duties, instead of repeating a generic monthly paragraph for the worker.

For personal/preferred requests that were not fulfilled:

- Keep them in the sheet even though completed assignments are now also explained.
- Give the actual causal reason, not merely the final roster condition observed after later assignments.
- Use concise reason categories: hard-rule conflict, unavailable, or sacrificed to a specifically named higher protected balancing priority.
- When balance caused the loss, name the priority, for example total-duty fairness, weekend/Friday balance, or Saturday balance. Do not write only `ויתור לטובת עדיפות איזון מוגנת גבוהה יותר`.
- Add a very short scope to a protected-balance explanation: state whether the change improved that worker's own burden or the balance of the group/other residents.
- If a non-protected soft preference caused the loss while the protected core stayed equal, name that soft preference and, when applicable, the alternate preferred date that was retained.
- When one resident-night slot on a date is fixed and the other was taken by an earlier preferred request, do not report that the requested date was fully occupied by fixed assignments. State that one slot was fixed and name the competing preferred requester who received the remaining slot. Use the fixed-slot explanation only when every compatible resident-night slot was actually fixed.
- If a preferred request was seeded first and later displaced, explain the displacement. Do not imply that a later adjacent duty existed before the preferred request was considered.

Do not duplicate monthly summary metrics already displayed in `תורנויות`.

## Workbook Invariants

- The existing summary panel in `תורנויות` works correctly. Do not modify or redesign it unless explicitly requested.
- `מתמחים פנויים` must remain a live workbook formula that updates from the current assignments, analogous to `בכירים פנויים`.
- `הסבר תורנויות` is a detailed assignment-and-exception audit sheet, not a second statistical summary sheet.
- The default print area of `תורנויות` includes only columns `A:F`; availability helpers and balancing summaries remain on screen but outside the printed roster.
- Keep irrelevant duplicate columns out of `הסבר תורנויות`, including duplicate sandwich, duty-total, weekend, or duty-type summaries already available in `תורנויות`.

## GUI Progress and Stage Titles

- The GUI title must describe the roster stage that is actually running.
- Progress percentages should be weighted by expected computational work, including progress within long optimization stages, rather than merely by the number of top-level functions completed.
- Avoid misleading long plateaus such as quickly reaching the middle percentage and remaining there for most of the run.
- Percentages must remain monotonic. They are work estimates, not a promise of exact wall-clock time.

## Execution and Verification

- Do not run a full monthly roster generation or live Google-backed end-to-end run unless the user explicitly asks for that specific run.
- Normal implementation verification should use focused unit tests, compilation/static checks, and small targeted scripts.
- The scheduler is already operational. Make focused improvements to the current algorithm rather than rebuilding it from scratch.
