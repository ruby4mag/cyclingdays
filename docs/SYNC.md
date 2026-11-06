# Synchronization: icTrainer, COROS and Intervals.icu

_Last updated: Phase 6 (2026-11-05). Verification `v1`, dedup `v1`._

CycleDays talks **only to Intervals.icu**. icTrainer and COROS connect to
Intervals.icu through their own integrations, which the athlete links in the
apps. CycleDays doesn't call icTrainer or COROS, and doesn't assume that
every data type moves in both directions: each direction is checked
separately against data that actually arrived.

```
                 planned workouts                   planned workouts (configured)
  CycleDays ───────────────────► Intervals.icu ─────────────────────► COROS watch
      ▲   (events/bulk upsert)      │    ▲  ▲ ◄──────── activities ────────┘
      │                             │    │  └────────── wellness ──────────┘
      │      activities, wellness   │    │
      └─────────────────────────────┘    │ completed rides
                  sync                  │
                        planned ▼       │
                            icTrainer ──┘  (ERG on the smart trainer)
```

## 1. Directions and their status

Statuses: **verified** = seen in synchronized data; **confirmed** = the
athlete confirmed it by hand (not observable through the API);
**configured** = enabled in Intervals.icu, not observable through the API;
**not_observed** = enabled, no evidence yet; **not_configured**; and
**attention** = evidence of a problem.

Live state on 2026-11-05 (athlete `i780524`, `scripts.check_sync --sync`):

| Direction | Check | Status 2026-11-05 | Evidence used |
|---|---|---|---|
| CycleDays → Intervals.icu: planned workouts | (Phase 5) | ✅ verified | event 152154504 read back and verified step by step |
| COROS linked | `coros.link` | configured | `coros_user_id` set; `coros_sync_activities`, `coros_download_wellness`, `coros_upload_workouts` all true |
| Intervals.icu → icTrainer: planned workouts | `intervals_to_ictrainer.planned` | not_observed | An icTrainer ride paired with a published CycleDays event **and carrying the planned workout's name**. Otherwise a manual confirmation |
| icTrainer: ERG executed | `ictrainer.erg_execution` | not_observed | That ride: indoor, has power, average power within ±10 % of planned and ≥ 80 % of the planned duration. Intervals.icu's `compliance` is recorded alongside |
| icTrainer → Intervals.icu: completed rides | `ictrainer_to_intervals.activities` | not_observed | An activity whose OAuth client or device name contains "icTrainer" |
| COROS → Intervals.icu: activities | `coros_to_intervals.activities` | not_observed | An activity with `source = COROS` |
| COROS → Intervals.icu: wellness | `coros_to_intervals.wellness` | ✅ **verified** | Device wellness on 26 of 28 days, latest 2026-11-04: `restingHR`, `hrv`, `sleepSecs`, `avgSleepingHR` (13 days each) and `steps` (26) |
| Intervals.icu → COROS: planned workouts | `intervals_to_coros.workouts` | configured | `coros_upload_workouts = true`. What reaches the watch isn't visible through the API |
| Same ride recorded twice | `activities.duplicates` | verified (none) | Overlapping recordings; see §3 |

**Not synchronized, and not assumed:**

* **icTrainer → wellness:** no wellness data comes from icTrainer.
* **COROS → planned workouts:** workouts made on the COROS side don't reach
  Intervals.icu.
* **Intervals.icu → icTrainer:** completed activities aren't sent to icTrainer.
* **COROS wellness fields never seen in 28 days:** `hrvSDNN`, `sleepScore`,
  `sleepQuality`, `spO2`, `respiration`, `readiness`, `weight` and `vo2max`.
  The readiness model already treats sleep score as missing.
* **Subjective wellness** (fatigue, soreness, stress, injury) is typed into
  Intervals.icu by the athlete; no device delivers it.

## 2. How each direction is checked

Run `python -m scripts.check_sync --sync`. It syncs from Intervals.icu,
evaluates every check over the last 28 days, and stores a row in
`coach.sync_checks` **only when a result changes**, so the table is a
history of transitions. Exit code 4 means at least one check needs attention.

* **Provenance.** Every activity now stores where it came from (migration
  0004, back-filled from the stored payloads):
  * `origin`: Intervals.icu `source`, e.g. `COROS`, `OAUTH_CLIENT`, `UPLOAD`;
  * `origin_client`: `oauth_client_name`;
  * `device_name`;
  * `origin_external_id`;
  * `paired_event_id`: the planned event Intervals.icu paired the ride with.
* **icTrainer** is identified by "icTrainer" in the OAuth client or device
  name. Its exact `source` value and client name are **not yet confirmed on
  real data**. The evidence block lists every source seen, so a different
  label would show up there.
* **Calendar → icTrainer can't be observed directly.** Intervals.icu pairs
  rides with planned events by date, so pairing alone doesn't prove that
  icTrainer loaded the workout. A matching name does. Otherwise, confirm it
  by hand:

  ```bash
  python -m scripts.check_sync --confirm intervals_to_ictrainer.planned \
      --note "Gentle Z1 Recovery Spin listed in icTrainer on 2026-11-06"
  ```

  These checks can be confirmed: `intervals_to_ictrainer.planned`,
  `ictrainer.erg_execution` and `intervals_to_coros.workouts`. Data evidence
  always overrides a confirmation, and a confirmation never turns on an
  integration that is off.
* **Wellness attribution.** Intervals.icu wellness records carry **no
  source field**. Wellness is attributed to COROS because it is the only
  wellness provider enabled (Garmin, Polar and Suunto wellness download are
  off). If another provider is enabled, the check reports the attribution as
  ambiguous. Data older than 3 days is reported as `attention`.

## 3. Duplicate recordings (`app/training/activity_dedup.py`)

COROS receives the planned workout, so the athlete may start it on the watch
while icTrainer records the same ride. Intervals.icu would then hold two
activities, and counting both would **double the training load**.
Intervals.icu's own duplicate handling isn't documented, so CycleDays
doesn't rely on it.

* **Same session:** the same sport family (every `*Ride` type is cycling) and
  a time overlap of at least 50 % of the shorter recording. Groups are
  transitive.
* **Kept as primary:** the recording with power, then the one paired with a
  planned workout, then the longest moving time, then the lowest id. The
  others get `duplicate_of_activity_id`.
* **Effect:** duplicates are kept, but excluded from training load. This is
  why `CALCULATION_VERSION` is now `v2`; on today's data v2 equals v1 on
  every day. The `activities.duplicates` check reports them as `attention`.
* **When it runs:** after every activity sync, over the synced window. If the
  primary disappears upstream, the link is cleared.

**Recommendation:** record indoor rides with icTrainer only. Don't also start
an activity on the watch, or keep the watch strictly as an HR sensor.

## 4. Known limitations

1. **Not observable through the Intervals.icu API:**
   * whether icTrainer shows the workout;
   * whether the COROS watch received it;
   * whether ERG mode was on (inferred from power matching the plan).

   These are confirmed by the athlete or inferred from the completed ride.
2. **COROS workout upload (forum reports, not official documentation):**
   * Intervals.icu uploads about **a week of workouts** as a bundle.
   * **Deletions may not reach the watch.**

   If CycleDays turns a day into a rest day and removes its event, a stale
   workout may stay on the watch. The Intervals.icu calendar is the
   authority. Turn off `coros_upload_workouts` if this causes confusion.

   **Observed 2026-11-05:** Friday's workout appeared in COROS Training Hub
   (training.coros.com) but **not in the COROS mobile app**. The app's
   training-plan library has no "Intervals.icu" plan. Forum reports say that
   Intervals.icu fills a placeholder plan named "Intervals.icu" in Training
   Hub (Calendar → Training Plans), which may need to be dragged onto the
   calendar. Ways to get the workout onto a COROS DURA:
   * In Training Hub, save the workout to the Workout Library. In the app,
     open Workouts → ⋯ → sync to the DURA. On the DURA, open Workouts. This
     is the documented COROS path.
   * Force a re-upload: move the workout to another day in Intervals.icu and
     back. Intervals.icu then re-sends the whole next week.
   * Disconnect and reconnect COROS in Intervals.icu (Settings →
     Connections), ticking every permission; check that there's no
     workout-type filter.

   **Single %FTP targets may arrive as 0 W (forum reports, Jan 2024 and Jan
   2025):** COROS reportedly needs a power *range* with different bounds.
   A fixed `70%` or `70-70%` shows as 0. CycleDays publishes steady steps as
   single values, so check the targets in the COROS app or on the DURA. If
   they show 0, a possible fix is publishing narrow ranges (e.g. `69-71%`);
   that needs the athlete's OK because it changes the Phase 5 publisher.
3. **icTrainer documentation is community-sourced.** Forum posts describe
   icTrainer pulling planned workouts automatically once Intervals.icu is
   linked, and offering to upload the ride afterwards. The real behaviour
   will be recorded here after the first ride.
4. **ERG tolerance.** The ±10 % average-power and 80 % duration rules are
   plausibility checks, not an evaluation. Step-by-step planned-versus-actual
   analysis is Phase 7.
5. **Sources older than this account:**
   * A 2023 forum post says COROS doesn't sync HRV; this account **does**
     receive `hrv`.
   * The same thread says COROS delivers steps; observed here too.
6. **Activity `source` values and `oauth_client_name` for icTrainer and COROS
   rides** still have to be seen on real data. Run `check_sync --sync` after
   the first ride.

## 5. Athlete checklist (one-time verification)

1. In icTrainer, link Intervals.icu (icTrainer's Intervals.icu settings;
   icTrainer publishes a tutorial video).
2. Open icTrainer on the workout day. The CycleDays workout (e.g. "Gentle Z1
   Recovery Spin") should be listed. Then run:

   ```bash
   check_sync --confirm intervals_to_ictrainer.planned --note "…"
   ```

3. Ride it in **ERG mode**, with icTrainer as the only recorder, and upload it
   to Intervals.icu when prompted.
4. Record any ride with the COROS watch to verify COROS activity sync.
5. Run `python -m scripts.check_sync --sync`. Expect `verified` for
   `ictrainer_to_intervals.activities`, `intervals_to_ictrainer.planned`,
   `ictrainer.erg_execution` and `coros_to_intervals.activities`.

## Sources

* Intervals.icu forum, COROS sync threads (2026-11-05):
  [plan created but workouts not syncing](https://forum.intervals.icu/t/coros-training-plan-is-created-but-planed-workouts-are-not-syncing/131097),
  [sync not working](https://forum.intervals.icu/t/coros-sync-from-intervals-not-working/132316),
  [VirtualRide workout shows 0 FTP in Coros](https://forum.intervals.icu/t/virtualride-workout-does-not-show-correctly-in-coros-app/56605).
* COROS: [Creating and Following Workouts on DURA](https://support.coros.com/hc/en-us/articles/27064483966100-Creating-and-Following-Workouts-on-DURA).

* Intervals.icu OpenAPI (`/api/v1/docs`, 2026-11-05): Activity `source`
  enum, `oauth_client_name`, `device_name`, `paired_event_id`, `compliance`;
  athlete `coros_*` settings; Wellness fields.
* [Coros support added (Intervals.icu forum)](https://forum.intervals.icu/t/coros-support-added/37006)
  (developer posts, April–July 2023).
* [Coros and structured workouts (Intervals.icu forum)](https://forum.intervals.icu/t/coros-and-structured-workouts/16859).
* [New: icTrainer … (Intervals.icu forum)](https://forum.intervals.icu/t/new-ictrainer-is-an-affordable-alternative-to-rouvy-zwift-co/8231?page=3)
  and [Indoor training app summary](https://forum.intervals.icu/t/indoor-training-app-summary/22448).
