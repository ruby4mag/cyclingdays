// Account and onboarding page (Phase 10). No dependencies; same-origin JSON only.
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const ROLE_LABEL = { rest: "Rest", recovery: "Recovery", endurance: "Endurance", key: "Key", long: "Long" };
  const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
  const DEFAULT_WEEK = { mon: "rest", tue: "key", wed: "endurance", thu: "key", fri: "recovery", sat: "long", sun: "endurance" };
  let account = null;
  let editingConnection = false;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c);
    return node;
  }

  async function api(method, path, body) {
    const opts = { method, headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch("api/account" + path, opts);
    if (res.status === 401) { window.location.assign("login"); throw new Error("Signed out"); }
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) throw new Error(errorText(data, res.status));
    return data;
  }

  function errorText(data, status) {
    const d = data && data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length) {
      return d.map((e) => {
        const field = (e.loc || []).filter((p) => p !== "body").join(" ");
        return (field ? field.replace(/_/g, " ") + ": " : "") + (e.msg || "invalid");
      }).join("; ");
    }
    return status >= 500 ? "The server had a problem; try again." : "That didn't work (" + status + ").";
  }

  function setMsg(form, text, kind) {
    const msg = $(".form-msg", form);
    msg.textContent = text || "";
    msg.className = "form-msg" + (kind ? " " + kind : "");
  }

  function busy(form, on) {
    form.querySelectorAll("button").forEach((b) => { b.disabled = on; });
  }

  async function submit(form, work, okText) {
    setMsg(form, "");
    busy(form, true);
    try {
      const result = await work();
      if (okText) setMsg(form, okText, "ok");
      return result;
    } catch (err) {
      setMsg(form, err.message, "error");
      return null;
    } finally {
      busy(form, false);
    }
  }

  function num(value) {
    if (value === "" || value === null || value === undefined) return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function fmtDate(iso) {
    if (!iso) return "–";
    const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }

  // --- render ---------------------------------------------------------------

  function render(data) {
    account = data;
    const setup = data.setup;
    const who = $("#signed-in-as");
    const avatar = $("#avatar");
    who.textContent = data.user.username;
    avatar.textContent = data.user.username.charAt(0).toUpperCase();

    const steps = $("#steps");
    const order = ["connected", "profile", "goal"];
    const current = order.find((s) => !setup[s]);
    steps.hidden = setup.complete;
    steps.querySelectorAll("li").forEach((li) => {
      const s = li.dataset.step;
      li.classList.toggle("done", Boolean(setup[s]));
      li.classList.toggle("current", s === current);
    });

    $("#hero-eyebrow").textContent = setup.complete ? "Account" : "Get started";
    $("#hero-h").textContent = setup.complete ? "Your coach settings" : "Set up your coach";
    $("#hero-sub").textContent = setup.complete
      ? "Change your connection, profile or goal here. A new goal rebuilds your plan from today."
      : "Three steps: connect Intervals.icu, check your profile, then set your goal. Your first workout follows within minutes.";
    const nav = $("#nav-dashboard");
    if (data.athlete) nav.removeAttribute("aria-disabled");
    else nav.setAttribute("aria-disabled", "true");

    const banner = $("#banner");
    banner.replaceChildren();
    banner.hidden = true;
    if (data.connection && data.connection.status !== "active") {
      banner.hidden = false;
      banner.append("Intervals.icu stopped accepting your API key, so coaching is paused. Paste a new key below.");
    } else if (setup.complete && new URLSearchParams(window.location.search).has("welcome")) {
      banner.hidden = false;
      banner.append("All set. Your history is syncing and your first workout is being planned; it appears on your Intervals.icu calendar shortly. ",
        el("a", { href: "dashboard", text: "Open the dashboard" }));
    }

    renderConnection(data);
    renderProfile(data);
    renderGoal(data);
    renderNotify(data);
    lock("#card-profile", !data.athlete);
    lock("#card-goal", !data.athlete || !setup.profile);
  }

  function lock(sel, locked) {
    const card = $(sel);
    card.classList.toggle("locked", locked);
    card.querySelectorAll("input, select, button").forEach((n) => { n.disabled = locked; });
  }

  function renderConnection(data) {
    const conn = data.connection;
    const status = $("#conn-status");
    const form = $("#form-intervals");
    status.replaceChildren();
    if (conn) {
      const ok = conn.status === "active";
      status.hidden = false;
      status.append(
        el("span", { class: "badge" }, el("span", { class: "status-dot " + (ok ? "s-good" : "s-critical") }), ok ? "Connected" : "Key refused"),
        el("span", {}, "Athlete ", el("strong", { text: conn.intervals_athlete_id })),
        el("span", { class: "muted small", text: data.athlete && data.athlete.last_sync_at ? "Last sync " + fmtDate(data.athlete.last_sync_at) : "First sync pending" }),
      );
      const actions = el("span", { class: "actions" },
        el("button", { type: "button", class: "ghost", id: "conn-replace", text: "Replace key" }),
        el("button", { type: "button", class: "ghost", id: "conn-disconnect", text: "Disconnect" }));
      status.append(actions);
      $("#conn-replace", status).addEventListener("click", () => { editingConnection = true; renderConnection(account); });
      $("#conn-disconnect", status).addEventListener("click", confirmDisconnect);
      form.hidden = ok && !editingConnection;
      form.elements.athlete_id.value = conn.intervals_athlete_id;
      form.elements.athlete_id.readOnly = true;
      $("#conn-cancel").hidden = !(ok && editingConnection);
    } else {
      status.hidden = true;
      form.hidden = false;
      form.elements.athlete_id.readOnly = false;
      $("#conn-cancel").hidden = true;
    }
    form.elements.api_key.value = "";
  }

  function renderProfile(data) {
    const a = data.athlete;
    const form = $("#form-profile");
    const select = $("#experience");
    if (!select.options.length) {
      for (const lvl of data.options.experience_levels) {
        select.append(el("option", { value: lvl, text: lvl.charAt(0).toUpperCase() + lvl.slice(1) }));
      }
    }
    if (!a) return;
    for (const name of ["display_name", "ftp_watts", "weight_kg", "height_cm", "age_years", "timezone", "indoor_platform", "wearable", "secondary_objective", "experience_level"]) {
      const field = form.elements[name];
      field.value = a[name] === null || a[name] === undefined ? "" : a[name];
    }
    const note = $("#ftp-note");
    if (a.intervals_ftp_watts && a.intervals_ftp_watts !== a.ftp_watts) {
      note.textContent = "Intervals.icu has " + a.intervals_ftp_watts + " W";
    } else if (!a.ftp_watts) {
      note.textContent = "Needed: no FTP in Intervals.icu. A recent 20-min test × 0.95 works.";
    } else {
      note.textContent = "";
    }
  }

  function renderGoal(data) {
    const form = $("#form-goal");
    const grid = $("#week-grid");
    const goal = data.goal;
    const week = (goal && goal.week_template) || DEFAULT_WEEK;
    if (!grid.children.length) {
      for (const day of data.options.weekdays) {
        const select = el("select", { name: "day_" + day, "aria-label": DAY_LABEL[day] });
        for (const role of data.options.day_roles) select.append(el("option", { value: role, text: ROLE_LABEL[role] || role }));
        grid.append(el("label", {}, DAY_LABEL[day], select));
      }
    }
    for (const day of data.options.weekdays) form.elements["day_" + day].value = week[day] || "endurance";

    const current = $("#goal-current");
    current.replaceChildren();
    if (goal) {
      current.hidden = false;
      const hours = goal.weekly_hours ? goal.weekly_hours[0] + "–" + goal.weekly_hours[1] + " h/week" : null;
      current.append(
        el("div", { class: "workout-name", text: goal.plan_name }),
        el("div", { class: "facts" },
          el("span", {}, "Objective ", el("strong", { text: goal.objective || "–" })),
          el("span", {}, "Event ", el("strong", { text: goal.event_date ? fmtDate(goal.event_date) : "none (open-ended)" })),
          hours ? el("span", {}, el("strong", { text: hours })) : null,
          el("span", { class: "muted", text: fmtDate(goal.start_date) + " → " + fmtDate(goal.end_date) })),
      );
      form.elements.objective.value = goal.objective || "";
      form.elements.event_date.value = goal.event_date || "";
      if (goal.weekly_hours) {
        form.elements.weekly_hours_min.value = goal.weekly_hours[0];
        form.elements.weekly_hours_max.value = goal.weekly_hours[1];
      }
      $("button[type=submit]", form).textContent = "Save new goal and rebuild plan";
    } else {
      current.hidden = true;
      if (data.athlete && data.athlete.primary_objective && !form.elements.objective.value) {
        form.elements.objective.value = data.athlete.primary_objective;
      }
    }
    const today = new Date();
    const iso = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    form.elements.event_date.min = iso;
  }

  function renderNotify(data) {
    const form = $("#form-notify");
    form.elements.email.value = data.user.email || "";
    form.elements.daily_summary.checked = Boolean(data.user.daily_summary);
  }

  function renderPreview(plan, saved) {
    const out = $("#goal-preview-out");
    out.hidden = false;
    out.replaceChildren();
    const rows = plan.weeks.map((w) => el("tr", {},
      el("td", { text: fmtDate(w.start) }),
      el("td", { text: w.name }),
      el("td", { class: "type-chip", text: w.type }),
      el("td", { text: (w.key_session || "–").replace(/_/g, " ") }),
      el("td", { class: "num", text: w.tss === null ? "–" : Math.round(w.tss) }),
      el("td", { class: "num", text: w.minutes === null ? "–" : (w.minutes / 60).toFixed(1) + " h" })));
    out.append(
      el("h3", { text: (saved ? "Saved: " : "Preview: ") + plan.name }),
      el("p", { class: "muted small", text: plan.weeks.length + " weeks, " + fmtDate(plan.start_date) + " → " + fmtDate(plan.end_date) +
        (saved ? "" : ". Nothing is stored until you save.") }),
      el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ...["Week of", "Week", "Type", "Key session", "TSS", "Time"].map((h, i) =>
          el("th", { class: i >= 4 ? "num" : null, text: h })))),
        el("tbody", {}, ...rows))));
  }

  // --- actions ----------------------------------------------------------------

  function goalBody(form) {
    const week = {};
    for (const day of account.options.weekdays) week[day] = form.elements["day_" + day].value;
    return {
      objective: form.elements.objective.value.trim(),
      event_date: form.elements.event_date.value || null,
      weekly_hours_min: num(form.elements.weekly_hours_min.value),
      weekly_hours_max: num(form.elements.weekly_hours_max.value),
      start_date: form.elements.start_date.value || null,
      week_template: week,
    };
  }

  function confirmDisconnect() {
    const dialog = $("#confirm-disconnect");
    dialog.returnValue = "";
    dialog.addEventListener("close", async () => {
      if (dialog.returnValue !== "ok") return;
      try {
        render(await api("DELETE", "/intervals"));
      } catch (err) {
        setMsg($("#form-intervals"), err.message, "error");
      }
    }, { once: true });
    dialog.showModal();
  }

  function wire() {
    const intervals = $("#form-intervals");
    intervals.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { athlete_id: intervals.elements.athlete_id.value.trim(), api_key: intervals.elements.api_key.value.trim() };
      if (!body.athlete_id || !body.api_key) { setMsg(intervals, "Enter the athlete ID and the API key.", "error"); return; }
      setMsg(intervals, "Checking with Intervals.icu…");
      const data = await submit(intervals, () => api("PUT", "/intervals", body));
      if (data) { editingConnection = false; render(data); setMsg($("#form-profile"), data.setup.profile ? "" : "Check your profile, then save it.", ""); }
    });
    $("#conn-cancel").addEventListener("click", () => { editingConnection = false; renderConnection(account); });

    const profile = $("#form-profile");
    profile.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = profile.elements;
      const body = {
        display_name: f.display_name.value.trim(),
        ftp_watts: num(f.ftp_watts.value),
        weight_kg: num(f.weight_kg.value),
        height_cm: num(f.height_cm.value),
        age_years: num(f.age_years.value),
        experience_level: f.experience_level.value,
        timezone: f.timezone.value.trim(),
        secondary_objective: f.secondary_objective.value.trim() || null,
        indoor_platform: f.indoor_platform.value.trim() || null,
        wearable: f.wearable.value.trim() || null,
      };
      if (!body.ftp_watts) { setMsg(profile, "FTP is needed to size your workouts.", "error"); f.ftp_watts.focus(); return; }
      const data = await submit(profile, () => api("PUT", "/profile", body), "Profile saved.");
      if (data) render(data);
    });

    const goal = $("#form-goal");
    $("#goal-preview").addEventListener("click", async () => {
      const plan = await submit(goal, () => api("POST", "/goal/preview", goalBody(goal)));
      if (plan) renderPreview(plan, false);
    });
    goal.addEventListener("submit", async (e) => {
      e.preventDefault();
      const firstPlan = !account.setup.goal;
      const result = await submit(goal, () => api("PUT", "/goal", goalBody(goal)), "Plan saved.");
      if (!result) return;
      renderPreview(result, true);
      if (firstPlan && result.account.setup.complete) {
        window.history.replaceState(null, "", "account?welcome");
      }
      render(result.account);
    });

    const notify = $("#form-notify");
    notify.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { email: notify.elements.email.value.trim() || null, daily_summary: notify.elements.daily_summary.checked };
      const data = await submit(notify, () => api("PUT", "/notifications", body), "Saved.");
      if (data) render(data);
    });
  }

  function timezones() {
    const list = $("#timezones");
    try {
      for (const tz of Intl.supportedValuesOf("timeZone")) list.append(el("option", { value: tz }));
    } catch (_) { /* older browser: free text */ }
  }

  timezones();
  wire();
  api("GET", "")
    .then((data) => {
      render(data);
      if (!data.athlete) {
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (tz) $("#form-profile").elements.timezone.value = tz;
      }
    })
    .catch((err) => { $("#hero-sub").textContent = err.message; });
})();
