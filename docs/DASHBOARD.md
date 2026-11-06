# Dashboard

_Last updated: Phase 8 (2026-11-05). `dashboard/v1`, evaluation `evaluation/v2`._

Open **http://127.0.0.1:8010/dashboard** (`/` redirects there). The dashboard
is read-only: it never calls Intervals.icu or OpenAI and never writes to the
database, so opening it can't change the plan.

```
browser ──► GET /dashboard                    static page (HTML + 1 JS + 1 CSS, no build step)
        ──► GET /api/dashboard/<section>      JSON, one request per section
                    │
                    ▼
          app/services/dashboard.py           pure aggregations + read-only queries
                    │
                    ▼
          PostgreSQL (coach schema)           the same rows the coach uses
```

## 1. What it shows

| Spec item | Where | Source |
|---|---|---|
| Athlete profile | "Athlete profile" card | `athlete_profile` (the database is authoritative, not the seed values) |
| Current FTP | first tile and profile card: W, W/kg, the 7 power zones in watts, and whether Intervals.icu has the same FTP (if not, publishing is blocked, see WORKOUTS.md) | `athlete_profile.ftp_watts`, Intervals.icu `sportSettings` snapshot |
| Fitness and fatigue trends | CTL/ATL line chart and a form (TSB) chart; tiles with the latest values | `training_metrics` (current calculation version) |
| Training calendar | 5 weeks (2 back, 3 ahead): the plan's day role, the coach's workout and its status, rides with their outcome, and other Intervals.icu calendar events | plan template, `planned_workouts`, `activities`, `workout_performance`, `calendar_events` |
| Upcoming workout | the next non-rest coach workout within 14 days: target-power profile, steps, TSS, IF, published status, and why it was chosen | `planned_workouts.structure` |
| Completed workouts | table of rides: time, distance, TSS, IF, power, HR, RPE, the planned workout, outcome, completion, power against target, HR drift, and the coach's recommendation; ride reviews | `activities`, `workout_performance` |
| Weekly training volume | TSS per week (Mon-Sun) with the plan's target; hours, rides and distance in the tooltip and table | `training_metrics.daily_tss`, `activities`, plan |
| Power zone distribution | share of riding time per zone over the range | `workout_performance.evaluation.zones`; Intervals.icu `icu_zone_times` as fallback |
| AI coaching assessments | the last 10 daily plans (objective, rationale, recovery and training assessment, fallback/abort reasons, model, prompt versions) and ride reviews | `coach_decisions` (`daily_plan`, `performance_review`) |
| Long-term objective progress | the active plan: current week, key session, weeks done, compliance of finished weeks, CTL since the start, and every week's target against what was done | `training_plans`, `training_metrics`, `activities` |

Also shown: readiness for the last planned day (status, confidence,
intensity limit, every cap and signal), sync status per resource, and the
latest result of each device and app check (Phase 6).

The range buttons (4 weeks, 12 weeks, 6 months, 1 year) scope the trends,
weekly volume, zones and completed workouts together. The choice is
remembered in the browser only.

## 2. Honesty rules

* **Provisional data looks provisional.** Days where the training engine
  marks CTL or TSB as not authoritative get a grey band on the fitness chart
  and faded columns on the form chart. The tiles say "provisional: not enough
  history", and the engine's reason is printed under the chart.
* **Missing is not zero.** A week without metric rows shows "—", not 0 TSS.
  Compliance is only computed for finished weeks.
* **Zones say where they come from.** The coach's own zones (from the power
  stream, at the FTP of the ride, evaluation v2) are preferred. Rides without
  them fall back to Intervals.icu's `icu_zone_times`, which uses the
  athlete's Intervals.icu zone settings. The footnote counts the rides from
  each source and the rides without zone data. Ids other than Z1-Z7 (e.g. a
  sweet-spot band) are ignored.
* **Duplicates are hidden.** The same ride recorded twice (Phase 6) is
  counted once everywhere, and the workouts table says how many were hidden.

## 3. API

All endpoints are `GET`, return JSON with `Cache-Control: no-store`, and
show the athlete `ATHLETE_SLUG` (default `primary`). The response is 404 if
the athlete doesn't exist and 422 for a range outside the limits.

| Endpoint | Parameters (default, limit) |
|---|---|
| `/api/dashboard/summary` | — |
| `/api/dashboard/fitness` | `days` (90, ≤ 365) |
| `/api/dashboard/weekly` | `weeks` (12, ≤ 52) |
| `/api/dashboard/zones` | `days` (28, ≤ 365) |
| `/api/dashboard/workouts` | `days` (28, ≤ 365) |
| `/api/dashboard/calendar` | `start`, `end` (2 weeks back to 3 weeks ahead, ≤ 62 days) |
| `/api/dashboard/assessments` | `limit` (10, ≤ 50) |
| `/api/dashboard/plan` | — |

Interactive documentation: http://127.0.0.1:8010/docs.

## 4. Frontend

* **Plain HTML, one JavaScript file and one CSS file** in `app/web/`, served
  by FastAPI (`StaticFiles`). No framework, build step, CDN or new
  dependency; the charts are drawn as SVG by `dashboard.js`.
* **Charts:**
  * one y-axis per chart; thin marks;
  * a legend for every chart with two or more series;
  * selective direct labels;
  * a tooltip on hover and keyboard focus (arrow keys move the crosshair
    on line charts);
  * every chart has a "Show as table" view.
* **Colors:**
  * Light and dark mode follow the operating system; dark mode has its own
    color steps.
  * Fitness and fatigue use blue and orange; form uses blue (fresh) and
    red (fatigued). Both pairs passed the colorblind and contrast checks in
    light and dark mode.
  * Status colors always come with a text label.
* **Layout** works down to phone width: the calendar becomes a list and
  wide tables scroll.

### Security

* **Same-origin only:** the page's CSP is `default-src 'none'; script-src
  'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'`, with
  no inline scripts or styles. Every response also gets `nosniff`,
  `Referrer-Policy: same-origin` (nothing is sent to other sites) and `X-Frame-Options: DENY`.
* **No HTML parsing of API data:** every string from the API is inserted
  with `textContent`. A unit test fails if `innerHTML`, `eval` or an
  external URL appears.
* **Login.** The app has its own sign-in (`/login`); see "Login" below.

### Login

* **Users** live in `coach.app_users` (migration 0006) and are managed from
  the command line. There is no sign-up page:

  ```bash
  podman exec -it cycling-coach_app_1 python -m scripts.manage_users add <username>
  podman exec -it cycling-coach_app_1 python -m scripts.manage_users passwd <username>
  podman exec -it cycling-coach_app_1 python -m scripts.manage_users disable <username>
  podman exec -it cycling-coach_app_1 python -m scripts.manage_users list
  ```

  Usernames are 1-64 characters (lowercase letters, digits, `.`, `_`, `-`).
  Passwords need at least 12 characters and are stored as scrypt hashes.
* **What is protected:** everything except `/`, `/login`, `/logout`,
  `/static/*` and the health checks. Without a session, `/dashboard` and
  `/docs` redirect to the login page and every other path answers 401 (the
  page then goes to the login page itself). Redirects are relative, so the
  app works under a reverse-proxy prefix such as `/cyclingdays/`.
* **Sessions** are signed cookies (HMAC-SHA256 with `AUTH_SECRET_KEY`),
  `HttpOnly`, `SameSite=Lax`, valid for `AUTH_SESSION_HOURS` (7 days).
  `Secure` is set when the request came over HTTPS (or always with
  `AUTH_COOKIE_SECURE=true`). Changing a user's password or disabling them
  ends all their sessions at once. Signing out deletes the cookie in that
  browser only.
* **Guessing** is throttled in memory: 5 failures per username, or 50 in total,
  within 15 minutes block further attempts until the window passes. Failed
  and successful sign-ins are logged with the username.
* **Cross-site posts** to `/login` and `/logout` are refused (`Origin` check).
* Without `AUTH_SECRET_KEY` a random key is used and everyone is signed out
  on each restart (a warning is logged).
* **Deployment:** https://cyclingdays.in/cyclingdays/ behind nginx with a
  Let's Encrypt certificate (`deploy/nginx/cyclingdays-tls.conf`) and
  `AUTH_COOKIE_SECURE=true`. Plain HTTP and access by IP redirect there.

## 5. Limitations

* **No real ride in the account yet,** so the completed-workouts table, the
  zone chart and the weekly volume are empty in real use. The tests and a
  seeded demo database (screenshots were checked) cover the populated views.
* **The zone fallback is unverified on real data.** The `icu_zone_times`
  format (`[{"id": "Z1", "secs": …}]`) comes from the OpenAPI schema.
* **Power zones exist only for rides evaluated since Phase 8** (evaluation
  v2), plus whatever Intervals.icu provides for older rides.
* **The page doesn't refresh itself.** Reload it to see new data; the
  scheduler polls every 30 minutes.
