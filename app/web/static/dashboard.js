/* CycleDays dashboard (Phase 8). No dependencies; charts are hand-built SVG.
 * Every string from the API is inserted with textContent and never parsed as HTML. */
"use strict";

(() => {
  // Relative, so the page also works behind a reverse proxy under a path prefix.
  const API = "api/dashboard";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const state = { days: 84, cache: {} };

  // ------------------------------------------------------------------ helpers

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      // CSSOM, not a style attribute: the page's CSP has no 'unsafe-inline'.
      else if (k === "style") node.style.cssText = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === "text") node.textContent = v;
      else node.setAttribute(k, String(v));
    }
    return node;
  }

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const fmt = (v, d = 0) =>
    isNum(v) ? v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
  const fmtSigned = (v, d = 0) => (isNum(v) ? (v > 0 ? "+" : "") + fmt(v, d) : "—");
  const pct = (v, d = 0) => (isNum(v) ? fmt(v, d) + " %" : "—");

  function parseDate(iso) {
    const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }
  function fmtDate(iso, opts) {
    if (!iso) return "—";
    return parseDate(iso).toLocaleDateString(undefined, {
      timeZone: "UTC", day: "numeric", month: "short", ...(opts || {}),
    });
  }
  function fmtDay(iso) {
    return fmtDate(iso, { weekday: "short" });
  }
  function fmtDateTime(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString(undefined, {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    });
  }
  function fmtDuration(seconds) {
    if (!isNum(seconds)) return "—";
    const m = Math.round(seconds / 60);
    const h = Math.floor(m / 60);
    return h ? `${h}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`;
  }
  const LABELS = { hrv: "HRV", resting_hr: "Resting HR", tss: "TSS", vo2max: "VO2max", ctl: "CTL", atl: "ATL", tsb: "TSB" };
  function humanize(s) {
    if (s && LABELS[s]) return LABELS[s];
    if (!s) return "—";
    const t = String(s).replace(/_/g, " ");
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  const OUTCOME = {
    as_planned: ["As planned", "s-good"],
    completed_with_deviation: ["With deviation", "s-warning"],
    partial: ["Partial", "s-serious"],
    abandoned: ["Abandoned", "s-critical"],
    unplanned: ["Unplanned", "s-neutral"],
  };
  const READINESS = { green: "s-good", amber: "s-warning", red: "s-critical" };
  const RECOMMENDATION = { keep: "s-good", reduce: "s-warning", recover: "s-serious" };
  const WORKOUT_STATUS = {
    published: "s-good", completed: "s-good", validated: "s-neutral", draft: "s-neutral",
    missed: "s-serious", rejected: "s-critical",
  };

  function badge(label, cls) {
    return el("span", { class: "badge" }, el("span", { class: `status-dot ${cls || "s-neutral"}`, "aria-hidden": "true" }), label);
  }
  function outcomeBadge(outcome) {
    if (!outcome) return el("span", { class: "muted", text: "not evaluated" });
    const [label, cls] = OUTCOME[outcome] || [humanize(outcome), "s-neutral"];
    return badge(label, cls);
  }
  function empty(text) {
    return el("p", { class: "empty", text });
  }
  function table(headers, rows) {
    const thead = el("thead", {}, el("tr", {}, headers.map((h) =>
      el("th", { class: h.num ? "num" : null, scope: "col", text: h.label }))));
    const tbody = el("tbody", {}, rows.map((r) => el("tr", {}, r.map((cell, i) => {
      const td = el("td", { class: headers[i] && headers[i].num ? "num" : null });
      if (cell instanceof Node) td.append(cell);
      else td.textContent = cell === null || cell === undefined ? "—" : String(cell);
      return td;
    }))));
    return el("div", { class: "table-wrap" }, el("table", {}, thead, tbody));
  }
  function tableView(headers, rows, label) {
    return el("details", { class: "table-view" }, el("summary", { text: label || "Show as table" }), table(headers, rows));
  }
  function legend(items) {
    return el("ul", { class: "legend" }, items.map((it) =>
      el("li", {}, el("span", { class: it.kind, style: it.color ? `background:${it.color}` : null, "aria-hidden": "true" }), it.label)));
  }
  function body(id) {
    return document.querySelector(`#${id} .body`);
  }
  function setBody(id, ...nodes) {
    const b = body(id);
    b.replaceChildren(...nodes.flat().filter(Boolean));
  }

  // ------------------------------------------------------------------ tooltip

  const tip = document.getElementById("tooltip");
  function showTip(x, y, title, rows, note) {
    tip.replaceChildren(el("div", { class: "tt-title", text: title }));
    for (const r of rows) {
      tip.append(el("div", { class: "tt-row" },
        r.color ? el("span", { class: "key-line", style: `background:${r.color}`, "aria-hidden": "true" }) : null,
        el("strong", { text: r.value }), el("span", { class: "muted", text: r.label })));
    }
    if (note) tip.append(el("div", { class: "tt-note", text: note }));
    tip.hidden = false;
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    let left = x + 14;
    let top = y - h - 10;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (left < 8) left = 8;
    if (top < 8) top = y + 16;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  }
  function hideTip() {
    tip.hidden = true;
  }
  function anchorOf(node) {
    const r = node.getBoundingClientRect();
    return [r.left + r.width / 2, r.top];
  }

  // ------------------------------------------------------------------ chart scaffolding

  function niceMax(v) {
    if (!(v > 0)) return 10;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  /** Upper bound on a clean step so the axis ends just above the data. */
  function niceDomain(max, count) {
    const stepSize = niceMax(Math.max(max, 1e-9) / (count || 4));
    return Math.max(stepSize, Math.ceil(max / stepSize) * stepSize);
  }
  function ticks(min, max, count) {
    const step = niceMax((max - min) / count);
    const out = [];
    for (let t = Math.ceil(min / step) * step; t <= max + 1e-9; t += step) out.push(Math.round(t * 1000) / 1000);
    return out;
  }
  function frame(container, height, margin) {
    const width = Math.max(280, container.clientWidth || 600);
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });
    return { root, width, height, m: margin, iw: width - margin.l - margin.r, ih: height - margin.t - margin.b };
  }
  function yAxis(f, y, values, format) {
    for (const t of values) {
      const yy = f.m.t + y(t);
      f.root.append(svg("line", { class: t === 0 ? "baseline" : "gridline", x1: f.m.l, x2: f.m.l + f.iw, y1: yy, y2: yy }));
      f.root.append(svg("text", { class: "tick", x: f.m.l - 6, y: yy + 4, "text-anchor": "end", text: format ? format(t) : fmt(t) }));
    }
  }
  function xDateTicks(f, dates, xOf, maxTicks) {
    const n = dates.length;
    const every = Math.max(1, Math.ceil(n / maxTicks));
    for (let i = 0; i < n; i += every) {
      f.root.append(svg("text", { class: "tick", x: f.m.l + xOf(i), y: f.height - 4, "text-anchor": "middle", text: fmtDate(dates[i]) }));
    }
  }
  /** Bar path: square at the baseline, 4px rounded at the data end. */
  function barPath(x, base, end, w) {
    const h = Math.abs(end - base);
    const r = Math.min(4, w / 2, h);
    if (h < 0.5) return "";
    if (end < base) {
      return `M${x},${base}V${end + r}Q${x},${end} ${x + r},${end}H${x + w - r}Q${x + w},${end} ${x + w},${end + r}V${base}Z`;
    }
    return `M${x},${base}V${end - r}Q${x},${end} ${x + r},${end}H${x + w - r}Q${x + w},${end} ${x + w},${end - r}V${base}Z`;
  }
  function hBarPath(x0, y, x1, h) {
    const w = x1 - x0;
    const r = Math.min(4, h / 2, w);
    if (w < 0.5) return "";
    return `M${x0},${y}H${x1 - r}Q${x1},${y} ${x1},${y + r}V${y + h - r}Q${x1},${y + h} ${x1 - r},${y + h}H${x0}Z`;
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** Lines over dates with a crosshair tooltip (pointer and arrow keys). */
  function lineChart(container, opts) {
    const { dates, series, provisional, label } = opts;
    const f = frame(container, opts.height || 240, { t: 10, r: 64, b: 22, l: 40 });
    const all = series.flatMap((s) => s.values.filter(isNum));
    const yMax = niceDomain(Math.max(10, ...all) * 1.05, 4);
    const y = (v) => f.ih - (v / yMax) * f.ih;
    const n = dates.length;
    const step = n > 1 ? f.iw / (n - 1) : 0;
    const xOf = (i) => (n > 1 ? i * step : f.iw / 2);
    f.root.setAttribute("aria-label", label);

    if (provisional) {
      let i = 0;
      while (i < n) {
        if (!provisional[i]) { i++; continue; }
        let j = i;
        while (j + 1 < n && provisional[j + 1]) j++;
        const x0 = f.m.l + Math.max(0, xOf(i) - step / 2);
        const x1 = f.m.l + Math.min(f.iw, xOf(j) + step / 2);
        f.root.append(svg("rect", { class: "provisional", x: x0, y: f.m.t, width: Math.max(1, x1 - x0), height: f.ih }));
        i = j + 1;
      }
    }
    yAxis(f, y, ticks(0, yMax, 4));
    xDateTicks(f, dates, xOf, Math.max(2, Math.floor(f.iw / 90)));

    const ends = [];
    for (const s of series) {
      let d = "";
      let pen = false;
      s.values.forEach((v, i) => {
        if (!isNum(v)) { pen = false; return; }
        d += `${pen ? "L" : "M"}${(f.m.l + xOf(i)).toFixed(1)},${(f.m.t + y(v)).toFixed(1)}`;
        pen = true;
      });
      f.root.append(svg("path", { class: "line", d, stroke: s.color }));
      const last = s.values.map((v, i) => [v, i]).filter(([v]) => isNum(v)).pop();
      if (last) ends.push({ s, v: last[0], y: f.m.t + y(last[0]), x: f.m.l + xOf(last[1]) });
    }
    // Direct end labels only when they don't collide; the legend always carries identity.
    ends.sort((a, b) => a.y - b.y);
    const collide = ends.some((e, i) => i > 0 && e.y - ends[i - 1].y < 14);
    for (const e of ends) {
      f.root.append(svg("circle", { cx: e.x, cy: e.y, r: 4, fill: e.s.color, stroke: cssVar("--surface"), "stroke-width": 2 }));
      if (!collide) f.root.append(svg("text", { class: "dlabel strong", x: e.x + 8, y: e.y + 4, text: `${fmt(e.v)} ${e.s.short}` }));
    }

    const cross = svg("line", { class: "crosshair", y1: f.m.t, y2: f.m.t + f.ih, visibility: "hidden" });
    const dots = series.map((s) => svg("circle", { r: 4, fill: s.color, stroke: cssVar("--surface"), "stroke-width": 2, visibility: "hidden" }));
    f.root.append(cross, ...dots);
    const hit = svg("rect", { class: "hit", x: f.m.l, y: f.m.t, width: f.iw, height: f.ih });
    f.root.append(hit);
    let current = n - 1;
    function show(i, clientX, clientY) {
      if (n === 0) return;
      current = Math.max(0, Math.min(n - 1, i));
      const x = f.m.l + xOf(current);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      series.forEach((s, k) => {
        const v = s.values[current];
        dots[k].setAttribute("visibility", isNum(v) ? "visible" : "hidden");
        if (isNum(v)) { dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", f.m.t + y(v)); }
      });
      const rect = f.root.getBoundingClientRect();
      const scale = rect.width / f.width;
      const px = clientX !== undefined ? clientX : rect.left + x * scale;
      const py = clientY !== undefined ? clientY : rect.top + f.m.t * scale + 20;
      showTip(px, py, fmtDay(dates[current]),
        series.map((s) => ({ color: s.color, value: fmt(s.values[current], 1), label: s.name })),
        provisional && provisional[current] ? "Provisional: not enough history yet" : null);
    }
    function hide() {
      cross.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      hideTip();
    }
    hit.addEventListener("pointermove", (ev) => {
      const rect = f.root.getBoundingClientRect();
      const sx = ((ev.clientX - rect.left) / rect.width) * f.width - f.m.l;
      show(step ? Math.round(sx / step) : 0, ev.clientX, ev.clientY);
    });
    hit.addEventListener("pointerleave", hide);
    f.root.setAttribute("tabindex", "0");
    f.root.addEventListener("focus", () => show(current));
    f.root.addEventListener("blur", hide);
    f.root.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowLeft") { show(current - 1); ev.preventDefault(); }
      if (ev.key === "ArrowRight") { show(current + 1); ev.preventDefault(); }
      if (ev.key === "Escape") hide();
    });
    container.replaceChildren(f.root);
  }

  /** Columns around zero (positive one hue, negative the other); per-column tooltip. */
  function divergingColumns(container, opts) {
    const { dates, values, provisional, label } = opts;
    const f = frame(container, opts.height || 160, { t: 10, r: 16, b: 28, l: 40 });
    const nums = values.filter(isNum);
    const extent = niceDomain(Math.max(10, ...nums.map(Math.abs)) * 1.05, 2);
    const y = (v) => f.ih / 2 - (v / extent) * (f.ih / 2);
    const n = dates.length;
    const band = f.iw / Math.max(1, n);
    const w = Math.min(24, Math.max(1, band - (band > 4 ? 2 : 0)));
    const xOf = (i) => i * band + band / 2;
    f.root.setAttribute("aria-label", label);
    yAxis(f, y, ticks(-extent, extent, 4), (t) => fmtSigned(t));
    xDateTicks(f, dates, xOf, Math.max(2, Math.floor(f.iw / 90)));
    const base = f.m.t + y(0);
    values.forEach((v, i) => {
      const x = f.m.l + xOf(i) - w / 2;
      const g = svg("g", {});
      if (isNum(v) && v !== 0) {
        g.append(svg("path", {
          class: "mark", d: barPath(x, base, f.m.t + y(v), w),
          fill: v > 0 ? "var(--div-pos)" : "var(--div-neg)",
          "fill-opacity": provisional && provisional[i] ? 0.45 : 1,
        }));
      }
      const hit = svg("rect", { class: "hit", x: f.m.l + i * band, y: f.m.t, width: Math.max(band, 1), height: f.ih });
      g.append(hit);
      const tipFor = (cx, cy) => showTip(cx, cy, fmtDay(dates[i]),
        [{ value: fmtSigned(v, 1), label: v > 0 ? "fresh" : v < 0 ? "fatigued" : "neutral" }],
        provisional && provisional[i] ? "Provisional: not enough history yet" : null);
      hit.addEventListener("pointermove", (ev) => { g.firstChild.classList && g.firstChild.classList.add("hover"); tipFor(ev.clientX, ev.clientY); });
      hit.addEventListener("pointerleave", () => { g.firstChild.classList && g.firstChild.classList.remove("hover"); hideTip(); });
      f.root.append(g);
    });
    container.replaceChildren(f.root);
  }

  /** Columns with an optional target tick per column; per-column tooltip and focus. */
  function columnChart(container, opts) {
    const { items, label } = opts;
    const f = frame(container, opts.height || 220, { t: 18, r: 12, b: 22, l: 40 });
    const vals = items.flatMap((it) => [it.value, it.target]).filter(isNum);
    const yMax = niceDomain(Math.max(10, ...vals) * 1.08, 4);
    const y = (v) => f.ih - (v / yMax) * f.ih;
    const n = items.length;
    const band = f.iw / Math.max(1, n);
    const w = Math.min(24, Math.max(2, band - 2));
    f.root.setAttribute("aria-label", label);
    yAxis(f, y, ticks(0, yMax, 4));
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(f.iw / 70))));
    items.forEach((it, i) => {
      const cx = f.m.l + i * band + band / 2;
      const g = svg("g", { tabindex: "0", role: "img", "aria-label": it.aria });
      if (isNum(it.value) && it.value > 0) {
        g.append(svg("path", { class: "mark", d: barPath(cx - w / 2, f.m.t + f.ih, f.m.t + y(it.value), w), fill: "var(--series-1)", "fill-opacity": it.partial ? 0.55 : 1 }));
      }
      if (isNum(it.target)) {
        g.append(svg("line", { x1: cx - w / 2 - 3, x2: cx + w / 2 + 3, y1: f.m.t + y(it.target), y2: f.m.t + y(it.target), stroke: "var(--text-primary)", "stroke-width": 2, "stroke-linecap": "round" }));
      }
      if (it.highlight && isNum(it.value) && it.value > 0) {
        // Anchored to the column top, so it can't be read as the target's value.
        f.root.append(svg("text", { class: "dlabel strong", x: cx, y: f.m.t + y(it.value) - 6, "text-anchor": "middle", text: fmt(it.value) }));
      }
      if (i % every === 0) {
        f.root.append(svg("text", { class: "tick", x: cx, y: f.height - 4, "text-anchor": "middle", text: it.tick }));
      }
      const hit = svg("rect", { class: "hit", x: cx - band / 2, y: f.m.t, width: band, height: f.ih });
      g.append(hit);
      const on = (x, yy) => { g.querySelector(".mark") && g.querySelector(".mark").classList.add("hover"); showTip(x, yy, it.title, it.rows, it.note); };
      const off = () => { g.querySelector(".mark") && g.querySelector(".mark").classList.remove("hover"); hideTip(); };
      hit.addEventListener("pointermove", (ev) => on(ev.clientX, ev.clientY));
      hit.addEventListener("pointerleave", off);
      g.addEventListener("focus", () => on(...anchorOf(hit)));
      g.addEventListener("blur", off);
      f.root.append(g);
    });
    container.replaceChildren(f.root);
  }

  /** Horizontal bars with the value at the tip. */
  function hBarChart(container, opts) {
    const { items, label } = opts;
    const rowH = 28;
    const labelW = Math.min(170, Math.max(110, (container.clientWidth || 400) * 0.32));
    const f = frame(container, items.length * rowH + 8, { t: 4, r: 110, b: 4, l: labelW });
    f.root.setAttribute("aria-label", label);
    const max = Math.max(...items.map((it) => it.value).filter(isNum), 0.0001);
    items.forEach((it, i) => {
      const yy = f.m.t + i * rowH;
      const h = Math.min(16, rowH - 8);
      const g = svg("g", { tabindex: "0", role: "img", "aria-label": `${it.label}: ${it.valueLabel}` });
      g.append(svg("text", { class: "dlabel", x: f.m.l - 10, y: yy + rowH / 2 + 4, "text-anchor": "end", text: it.label }));
      const x1 = f.m.l + (isNum(it.value) ? (it.value / max) * f.iw : 0);
      if (isNum(it.value) && it.value > 0) {
        g.append(svg("path", { class: "mark", d: hBarPath(f.m.l, yy + (rowH - h) / 2, x1, h), fill: "var(--series-1)" }));
      }
      g.append(svg("text", { class: "dlabel strong", x: x1 + 8, y: yy + rowH / 2 + 4, text: it.valueLabel }));
      const hit = svg("rect", { class: "hit", x: 0, y: yy, width: f.width, height: rowH });
      g.append(hit);
      const on = (x, y2) => showTip(x, y2, it.label, it.rows, it.note);
      hit.addEventListener("pointermove", (ev) => on(ev.clientX, ev.clientY));
      hit.addEventListener("pointerleave", hideTip);
      g.addEventListener("focus", () => on(...anchorOf(hit)));
      g.addEventListener("blur", hideTip);
      f.root.append(g);
    });
    f.root.prepend(svg("line", { class: "baseline", x1: f.m.l, x2: f.m.l, y1: f.m.t, y2: f.height - f.m.b }));
    container.replaceChildren(f.root);
  }

  /** Workout profile: one block per step, width = duration, height = % FTP. */
  function workoutProfile(container, steps, ftp) {
    const f = frame(container, 150, { t: 12, r: 36, b: 20, l: 40 });
    f.root.setAttribute("aria-label", "Workout profile: target power per step");
    const total = steps.reduce((a, s) => a + (s.duration_s || 0), 0);
    if (!total) { container.replaceChildren(); return; }
    const top = niceDomain(Math.max(110, ...steps.map((s) => s.high_pct || 0)) * 1.05, 3);
    const y = (v) => f.ih - (v / top) * f.ih;
    yAxis(f, y, ticks(0, top, 3), (t) => `${t}%`);
    const ftpY = f.m.t + y(100);
    f.root.append(svg("line", { x1: f.m.l, x2: f.m.l + f.iw, y1: ftpY, y2: ftpY, stroke: "var(--text-secondary)", "stroke-width": 1 }));
    f.root.append(svg("text", { class: "tick", x: f.m.l + f.iw + 4, y: ftpY + 4, text: "FTP" }));
    let t = 0;
    for (const s of steps) {
      const x0 = f.m.l + (t / total) * f.iw;
      const x1 = f.m.l + ((t + s.duration_s) / total) * f.iw;
      const level = ((s.low_pct || 0) + (s.high_pct || 0)) / 2;
      const w = Math.max(1, x1 - x0 - 2);
      const g = svg("g", { tabindex: "0", role: "img", "aria-label": `Step ${s.index}: ${humanize(s.kind)}, ${fmtDuration(s.duration_s)} at ${s.low_pct}%` });
      g.append(svg("path", { class: "mark", d: barPath(x0 + 1, f.m.t + f.ih, f.m.t + y(level), w), fill: "var(--series-1)" }));
      const hit = svg("rect", { class: "hit", x: x0, y: f.m.t, width: Math.max(4, x1 - x0), height: f.ih });
      g.append(hit);
      const watts = s.low_watts === s.high_watts ? `${s.low_watts} W` : `${s.low_watts}–${s.high_watts} W`;
      const pctLabel = s.low_pct === s.high_pct ? `${s.low_pct}% FTP` : `${s.low_pct}–${s.high_pct}% FTP`;
      const on = (x, yy) => showTip(x, yy, `Step ${s.index} · ${humanize(s.kind)}`,
        [{ value: fmtDuration(s.duration_s), label: "duration" }, { value: pctLabel, label: watts }], s.note);
      hit.addEventListener("pointermove", (ev) => on(ev.clientX, ev.clientY));
      hit.addEventListener("pointerleave", hideTip);
      g.addEventListener("focus", () => on(...anchorOf(hit)));
      g.addEventListener("blur", hideTip);
      f.root.append(g);
      t += s.duration_s;
    }
    f.root.append(svg("text", { class: "tick", x: f.m.l, y: f.height - 4, text: "0:00" }));
    f.root.append(svg("text", { class: "tick", x: f.m.l + f.iw, y: f.height - 4, "text-anchor": "end", text: fmtDuration(total) }));
    container.replaceChildren(f.root);
  }

  // ------------------------------------------------------------------ data

  async function getJSON(path) {
    const res = await fetch(API + path, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (res.status === 401) { toLogin(); throw new Error("Signed out"); }
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j && j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { /* keep status text */ }
      throw new Error(detail);
    }
    return res.json();
  }

  async function load(ids, path, render) {
    const cards = ids.map((id) => document.getElementById(id)).filter(Boolean);
    cards.forEach((c) => c.classList.add("loading"));
    try {
      const data = await getJSON(path);
      state.cache[path.split("?")[0]] = { data, render };
      render(data);
    } catch (err) {
      ids.forEach((id) => { const b = body(id); if (b) b.replaceChildren(el("p", { class: "error", text: `Couldn't load this section: ${err.message}` })); });
    } finally {
      cards.forEach((c) => c.classList.remove("loading"));
    }
  }

  // ------------------------------------------------------------------ sections

  function tile(label, value, unit, note, statusCls) {
    return el("div", { class: "tile" },
      el("div", { class: "label", text: label }),
      el("div", { class: "value" }, statusCls ? el("span", { class: `status-dot ${statusCls}`, style: "margin-right:8px;vertical-align:middle", "aria-hidden": "true" }) : null, value, unit ? el("small", { text: unit }) : null),
      note ? el("div", { class: "note", text: note }) : null);
  }

  function renderSummary(s) {
    const a = s.athlete;
    document.getElementById("athlete-line").textContent =
      `${a.name} · ${a.primary_objective || "no objective set"} · today ${fmtDay(s.today)}`;

    const ftp = s.ftp;
    const fit = s.fitness;
    const r = s.readiness;
    const ftpNote = ftp.matches_intervals_icu === false
      ? `Intervals.icu has ${ftp.intervals_icu_watts} W: publishing is blocked until they match`
      : ftp.matches_intervals_icu ? `${fmt(ftp.w_per_kg, 2)} W/kg · matches Intervals.icu` : `${fmt(ftp.w_per_kg, 2)} W/kg`;
    const prov = (ok) => (ok ? null : "provisional: not enough history");
    document.getElementById("tiles").replaceChildren(
      tile("Current FTP", fmt(ftp.watts), "W", ftpNote, ftp.matches_intervals_icu === false ? "s-critical" : null),
      tile("Fitness (CTL)", fit ? fmt(fit.ctl, 1) : "—", null, fit ? prov(fit.ctl_authoritative) || `as of ${fmtDate(fit.date)}` : "no metrics yet"),
      tile("Fatigue (ATL)", fit ? fmt(fit.atl, 1) : "—", null, fit ? prov(fit.atl_authoritative) || `7-day load ${fmt(fit.rolling_7d_tss)} TSS` : null),
      tile("Form (TSB)", fit ? fmtSigned(fit.tsb, 1) : "—", null, fit ? prov(fit.tsb_authoritative) || (fit.tsb >= 0 ? "fresh" : "carrying fatigue") : null),
      tile("Readiness", r ? humanize(r.status) : "—", null, r ? `limit: ${humanize(r.effective_cap || r.max_intensity)} · for ${fmtDate(r.plan_date)}` : "not assessed yet", r ? READINESS[r.status] || "s-neutral" : null),
      tile("Plan this week", s.plan && s.plan.phase ? humanize(s.plan.phase_type) : "—", null,
        s.plan && s.plan.phase ? `${s.plan.phase} · target ${fmt(s.plan.week_target_tss)} TSS · today: ${humanize(s.plan.role_today)}` : "no active plan"),
    );

    renderNextWorkout(s.next_workout);
    renderReadiness(r);
    renderProfile(a, ftp);
    renderSync(s.sync, s.sync_checks);
  }

  function renderNextWorkout(w) {
    if (!w) {
      setBody("next-workout", empty("No workout planned for the next two weeks. The scheduler plans tomorrow every evening."));
      return;
    }
    const pub = w.published ? badge("Published to Intervals.icu", "s-good") : badge(humanize(w.status), WORKOUT_STATUS[w.status]);
    const profile = el("div", { class: "chart" });
    const nodes = [
      el("div", { class: "workout-head" }, el("span", { class: "workout-name", text: w.name }), el("span", { class: "muted", text: fmtDay(w.date) }), pub),
      el("div", { class: "facts" },
        el("span", {}, "Type ", el("strong", { text: humanize(w.type) })),
        el("span", {}, "Duration ", el("strong", { text: fmtDuration((w.minutes || 0) * 60) })),
        el("span", {}, "TSS ", el("strong", { text: fmt(w.tss) })),
        el("span", {}, "IF ", el("strong", { text: fmt(w.intensity_factor, 2) })),
        el("span", {}, "FTP ", el("strong", { text: `${fmt(w.ftp_watts)} W` }))),
      profile,
      w.objective ? el("p", { class: "prose", text: w.objective }) : null,
      tableView(
        [{ label: "Step" }, { label: "Kind" }, { label: "Duration", num: true }, { label: "% FTP", num: true }, { label: "Watts", num: true }, { label: "Note" }],
        w.steps.map((s) => [s.index, humanize(s.kind), fmtDuration(s.duration_s),
          s.low_pct === s.high_pct ? `${s.low_pct}` : `${s.low_pct}–${s.high_pct}`,
          s.low_watts === s.high_watts ? `${s.low_watts}` : `${s.low_watts}–${s.high_watts}`, s.note || ""]),
        "Show steps"),
      w.rationale ? el("details", { class: "table-view" }, el("summary", { text: "Why this workout" }), el("p", { class: "prose", text: w.rationale })) : null,
    ];
    setBody("next-workout", nodes);
    const draw = () => workoutProfile(profile, w.steps, w.ftp_watts);
    draw();
    resizers.set("profile", draw);
  }

  function renderReadiness(r) {
    if (!r) {
      setBody("readiness", empty("Readiness is assessed when a day is planned."));
      return;
    }
    const factorRows = r.factors.map((f) => [humanize(f.name), f.status ? humanize(f.status) : "—", f.detail || ""]);
    setBody("readiness",
      el("div", { class: "workout-head" }, badge(humanize(r.status), READINESS[r.status] || "s-neutral"),
        el("span", { class: "muted", text: `confidence ${r.confidence || "—"} · for ${fmtDay(r.plan_date)}` })),
      el("p", { class: "prose" }, "Highest intensity allowed: ", el("strong", { text: humanize(r.effective_cap || r.max_intensity) })),
      r.caps.length ? el("ul", { class: "plain" }, r.caps.map((c) => el("li", { text: `${humanize(c.max_intensity)}: ${c.reason}` }))) : null,
      table([{ label: "Signal" }, { label: "Status" }, { label: "Detail" }], factorRows),
      el("p", { class: "small muted", style: "margin-top:8px", text: `Assessed ${fmtDateTime(r.assessed_at)}. Missing data never counts as ready.` }));
  }

  function renderProfile(a, ftp) {
    setBody("profile",
      el("dl", { class: "kv" }, [
        ["Name", a.name], ["Objective", a.primary_objective], ["Secondary", a.secondary_objective],
        ["Experience", humanize(a.experience_level)], ["Age", a.age_years ? `${a.age_years} years` : null],
        ["Weight", a.weight_kg ? `${fmt(a.weight_kg, 1)} kg` : null], ["Height", a.height_cm ? `${fmt(a.height_cm)} cm` : null],
        ["FTP", ftp.watts ? `${ftp.watts} W (${fmt(ftp.w_per_kg, 2)} W/kg)` : "not set"],
        ["FTP updated", ftp.updated_at ? fmtDateTime(ftp.updated_at) : null],
        ["Trainer", a.indoor_platform], ["Wearable", a.wearable], ["Timezone", a.timezone],
      ].flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v || "—" })])),
      ftp.zones.length ? el("h3", { style: "margin-top:14px", text: "Power zones" }) : null,
      ftp.zones.length ? table([{ label: "Zone" }, { label: "Name" }, { label: "Watts", num: true }],
        ftp.zones.map((z) => [`Z${z.zone}`, z.name, z.upper_watts === null ? `≥ ${z.lower_watts}` : `${z.lower_watts}–${z.upper_watts}`])) : null);
  }

  function renderSync(sync, checks) {
    const CHECK = { verified: "s-good", confirmed: "s-good", configured: "s-neutral", not_observed: "s-neutral", not_configured: "s-warning", attention: "s-critical" };
    setBody("sync",
      sync.length ? table([{ label: "Data" }, { label: "Last success" }, { label: "Status" }],
        sync.map((s) => [humanize(s.resource), fmtDateTime(s.last_success_at),
          s.last_error ? badge(`failing (${s.consecutive_failures}×)`, "s-critical") : badge("ok", "s-good")])) : empty("Never synchronized."),
      checks.length ? el("h3", { style: "margin-top:14px", text: "Device and app checks" }) : null,
      checks.length ? table([{ label: "Check" }, { label: "Status" }, { label: "Summary" }],
        checks.map((c) => [humanize(c.check.replace(/\./g, " · ")), badge(humanize(c.status), CHECK[c.status]), c.summary])) : null);
  }

  function renderFitness(f) {
    const pts = f.points;
    if (!pts.length) {
      setBody("fitness", empty("No training metrics in this range yet."));
      setBody("form", empty("No training metrics in this range yet."));
      return;
    }
    const dates = pts.map((p) => p.date);
    const provisional = pts.map((p) => !p.ctl_authoritative);
    const anyProv = provisional.some(Boolean);
    const latest = pts[pts.length - 1];
    const lineBox = el("div", { class: "chart" });
    setBody("fitness",
      lineBox,
      legend([
        { kind: "key-line", color: "var(--series-1)", label: "Fitness (CTL)" },
        { kind: "key-line", color: "var(--series-2)", label: "Fatigue (ATL)" },
        anyProv ? { kind: "key-band", label: "Provisional (not enough history)" } : null,
      ].filter(Boolean)),
      !latest.ctl_authoritative && latest.reasons.length ? el("p", { class: "small muted", style: "margin-top:6px", text: latest.reasons.join(" ") }) : null,
      tableView([{ label: "Date" }, { label: "CTL", num: true }, { label: "ATL", num: true }, { label: "TSB", num: true }, { label: "TSS", num: true }, { label: "Provisional" }],
        pts.slice().reverse().map((p) => [fmtDay(p.date), fmt(p.ctl, 1), fmt(p.atl, 1), fmtSigned(p.tsb, 1), fmt(p.daily_tss), p.ctl_authoritative ? "" : "yes"])));
    const drawLine = () => lineChart(lineBox, {
      dates, provisional, label: "Fitness and fatigue over time",
      series: [
        { name: "Fitness (CTL)", short: "CTL", color: "var(--series-1)", values: pts.map((p) => p.ctl) },
        { name: "Fatigue (ATL)", short: "ATL", color: "var(--series-2)", values: pts.map((p) => p.atl) },
      ],
    });
    drawLine();
    resizers.set("fitness", drawLine);

    const formBox = el("div", { class: "chart" });
    setBody("form", formBox,
      legend([
        { kind: "key-rect", color: "var(--div-pos)", label: "Fresh (positive)" },
        { kind: "key-rect", color: "var(--div-neg)", label: "Fatigued (negative)" },
        anyProv ? { kind: "key-rect", color: "var(--axis)", label: "Faded: provisional" } : null,
      ].filter(Boolean)));
    const drawForm = () => divergingColumns(formBox, { dates, values: pts.map((p) => p.tsb), provisional: pts.map((p) => !p.tsb_authoritative), label: "Form (TSB) per day" });
    drawForm();
    resizers.set("form", drawForm);
  }

  function weekItems(weeks, highlightLast) {
    const lastDone = weeks.map((w, i) => [w, i]).filter(([w]) => isNum(w.tss)).pop();
    return weeks.map((w, i) => ({
      value: w.tss,
      target: w.target_tss,
      partial: !w.finished,
      highlight: highlightLast && lastDone && lastDone[1] === i,
      tick: fmtDate(w.week_start),
      title: `Week of ${fmtDate(w.week_start)}${w.finished ? "" : " (in progress)"}`,
      aria: `Week of ${fmtDate(w.week_start)}: ${fmt(w.tss)} TSS, target ${fmt(w.target_tss)}`,
      rows: [
        { value: `${fmt(w.tss)} TSS`, label: "done" },
        { value: isNum(w.target_tss) ? `${fmt(w.target_tss)} TSS` : "—", label: "plan target" },
        { value: `${fmt(w.hours, 1)} h`, label: `${w.rides} ride${w.rides === 1 ? "" : "s"}, ${fmt(w.distance_km)} km` },
      ],
      note: [w.plan_phase, isNum(w.compliance_pct) ? `compliance ${pct(w.compliance_pct)}` : null, w.tss_complete || !w.finished ? null : "some days have no metrics"].filter(Boolean).join(" · ") || null,
    }));
  }

  function renderWeekly(data) {
    const weeks = data.weeks;
    if (!weeks.length) { setBody("weekly", empty("No weeks in range.")); return; }
    const box = el("div", { class: "chart" });
    const hasTarget = weeks.some((w) => isNum(w.target_tss));
    setBody("weekly", box,
      legend([{ kind: "key-rect", color: "var(--series-1)", label: "TSS done (faded: week in progress)" }, hasTarget ? { kind: "key-tick", label: "Plan target" } : null].filter(Boolean)),
      tableView([{ label: "Week of" }, { label: "TSS", num: true }, { label: "Target", num: true }, { label: "Hours", num: true }, { label: "Target h", num: true }, { label: "Rides", num: true }, { label: "km", num: true }, { label: "Compliance", num: true }],
        weeks.slice().reverse().map((w) => [fmtDate(w.week_start), fmt(w.tss), fmt(w.target_tss), fmt(w.hours, 1), fmt(w.target_hours, 1), w.rides, fmt(w.distance_km), pct(w.compliance_pct)])));
    const draw = () => columnChart(box, { items: weekItems(weeks, true), label: "Weekly training load against plan target" });
    draw();
    resizers.set("weekly", draw);
  }

  function renderZones(z) {
    if (!z.total_seconds) {
      setBody("zones", empty(z.rides
        ? `${z.rides} ride${z.rides === 1 ? "" : "s"} in range, none with power-zone data yet. Zones come from each ride's power stream once it is evaluated.`
        : "No rides in this range yet."));
      return;
    }
    const box = el("div", { class: "chart" });
    const sources = [];
    if (z.rides_coach) sources.push(`${z.rides_coach} from the coach's evaluation (power stream, current zones)`);
    if (z.rides_intervals_icu) sources.push(`${z.rides_intervals_icu} from Intervals.icu zone times (its zone settings may differ)`);
    if (z.rides_without_zones) sources.push(`${z.rides_without_zones} without zone data`);
    setBody("zones", box,
      el("p", { class: "small muted", style: "margin-top:6px", text: `${fmtDuration(z.total_seconds)} of riding over ${z.rides} ride${z.rides === 1 ? "" : "s"}: ${sources.join("; ")}.` }),
      tableView([{ label: "Zone" }, { label: "% FTP" }, { label: "Time", num: true }, { label: "Share", num: true }],
        z.zones.map((q) => [`Z${q.zone} ${q.name}`, q.upper_pct === null ? `> ${q.lower_pct}` : `${q.lower_pct}–${q.upper_pct}`, fmtDuration(q.seconds), pct(isNum(q.fraction) ? q.fraction * 100 : null)])));
    const draw = () => hBarChart(box, {
      label: "Share of riding time per power zone",
      items: z.zones.map((q) => ({
        label: `Z${q.zone} ${q.name}`,
        value: q.fraction,
        valueLabel: `${pct(isNum(q.fraction) ? q.fraction * 100 : null)} · ${fmtDuration(q.seconds)}`,
        rows: [{ value: fmtDuration(q.seconds), label: q.upper_pct === null ? `> ${q.lower_pct}% FTP` : `${q.lower_pct}–${q.upper_pct}% FTP` }],
      })),
    });
    draw();
    resizers.set("zones", draw);
  }

  function renderCalendar(c) {
    const grid = el("div", { class: "cal", role: "list" });
    for (const d of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) grid.append(el("div", { class: "cal-head", "aria-hidden": "true", text: d }));
    for (const day of c.days) {
      const blank = !day.workout && !day.rides.length && !day.events.length;
      const past = day.date < c.today;
      const cell = el("div", { class: `cal-day${day.is_today ? " today" : ""}${past ? " past" : ""}${blank ? " blank" : ""}`, role: "listitem" });
      const stateBadge = day.state === "completed" ? badge("Done", "s-good")
        : day.state === "missed" ? badge("Missed", "s-serious")
        : day.state === "planned" ? badge("Planned", "s-neutral")
        : null;
      cell.append(el("div", { class: "cal-date" }, el("span", { text: day.is_today ? `Today · ${fmtDate(day.date)}` : fmtDay(day.date) }), stateBadge));
      if (day.plan_role) cell.append(el("div", { class: "cal-role", text: `${day.plan_role} day` }));
      if (day.workout) {
        const w = day.workout;
        cell.append(el("div", { class: "cal-item" },
          el("div", { class: "t", text: w.type === "rest" ? "Rest day" : w.name }),
          w.type === "rest" ? null : el("div", { class: "muted", text: `${fmtDuration((w.minutes || 0) * 60)} · ${fmt(w.tss)} TSS · ${w.published ? "published" : w.status}` })));
      }
      for (const r of day.rides) {
        cell.append(el("div", { class: "cal-item" },
          el("div", { class: "t", text: `✓ ${r.name || humanize(r.type)}` }),
          el("div", { class: "muted", text: `${fmtDuration((r.minutes || 0) * 60)} · ${fmt(r.tss)} TSS${r.outcome ? " · " + (OUTCOME[r.outcome] || [r.outcome])[0] : ""}` })));
      }
      for (const e of day.events) {
        cell.append(el("div", { class: "cal-item muted", text: `${humanize(e.category)}: ${e.name || "untitled"}` }));
      }
      grid.append(cell);
    }
    setBody("calendar", grid,
      el("p", { class: "small muted cal-legend", text: "Roles come from the long-term plan's weekly template. Missed workouts are never moved to another day." }));
  }

  function renderWorkouts(w) {
    if (!w.workouts.length) {
      setBody("workouts", empty("No completed rides in this range yet. Rides appear here after they sync from icTrainer or COROS through Intervals.icu."));
      return;
    }
    const headers = [{ label: "Date" }, { label: "Ride" }, { label: "Time", num: true }, { label: "km", num: true }, { label: "TSS", num: true }, { label: "IF", num: true },
      { label: "Avg / NP W", num: true }, { label: "HR", num: true }, { label: "RPE", num: true }, { label: "Planned" }, { label: "Outcome" },
      { label: "Done", num: true }, { label: "Power vs target", num: true }, { label: "Drift", num: true }, { label: "Coach" }];
    const rows = w.workouts.map((r) => [
      fmtDay(r.date), r.name || humanize(r.type), fmtDuration((r.minutes || 0) * 60), fmt(r.distance_km, 1), fmt(r.tss), fmt(r.intensity_factor, 2),
      `${fmt(r.avg_power_watts)} / ${fmt(r.normalized_power_watts)}`, fmt(r.avg_hr_bpm), fmt(r.rpe), r.planned_name || "—", outcomeBadge(r.evaluated ? r.outcome : null),
      pct(r.completion_pct), r.power_deviation_pct === null ? "—" : fmtSigned(r.power_deviation_pct, 1) + " %", pct(r.decoupling_pct, 1),
      r.recommendation ? badge(humanize(r.recommendation), RECOMMENDATION[r.recommendation]) : "—",
    ]);
    const reviews = w.workouts.filter((r) => r.review_summary);
    setBody("workouts", table(headers, rows),
      w.duplicates_hidden ? el("p", { class: "small muted", style: "margin-top:6px", text: `${w.duplicates_hidden} duplicate recording(s) hidden (the same ride from two devices counts once).` }) : null,
      reviews.length ? el("details", { class: "table-view" }, el("summary", { text: `Ride reviews (${reviews.length})` }),
        reviews.map((r) => el("p", { class: "prose" }, el("strong", { text: `${fmtDay(r.date)} · ${r.name || ""}: ` }), r.review_summary))) : null);
  }

  function renderPlan(p) {
    const plan = p.plan;
    if (!plan) { setBody("plan", empty("No active long-term plan. Create one with: python -m scripts.create_plan --weekly-hours 6-9")); return; }
    const current = plan.current_index === null ? null : plan.weeks[plan.current_index];
    const eventLine = plan.open_ended
      ? "Open-ended: no event date yet, so the plan keeps extending in 4-week blocks."
      : `Event on ${fmtDate(plan.event_date, { year: "numeric" })}, ${Math.max(0, Math.round((parseDate(plan.event_date) - parseDate(p.today)) / 86400000))} days away.`;
    const box = el("div", { class: "chart" });
    const nodes = [
      el("div", { class: "workout-head" }, el("span", { class: "workout-name", text: plan.name }),
        plan.weekly_hours ? el("span", { class: "muted", text: `${plan.weekly_hours[0]}–${plan.weekly_hours[1]} h/week` }) : null),
      el("p", { class: "prose", text: `${plan.objective_detail || plan.objective}. ${eventLine}` }),
      el("div", { class: "facts" },
        el("span", {}, "Now ", el("strong", { text: current ? `${current.name} (${current.type})` : "—" })),
        el("span", {}, "Key session ", el("strong", { text: current ? humanize(current.key_session) : "—" })),
        el("span", {}, "Weeks done ", el("strong", { text: `${plan.weeks_done}` })),
        el("span", {}, "Compliance so far ", el("strong", { text: pct(plan.completed_weeks_compliance_pct) })),
        el("span", {}, "Fitness ", el("strong", { text: `${fmt(plan.ctl_at_start, 1)} → ${fmt(plan.ctl_now, 1)} CTL` }), plan.ctl_authoritative === false ? el("span", { class: "muted", text: " (provisional)" }) : null)),
    ];
    if (!plan.open_ended && plan.weeks_total) {
      const share = plan.weeks_done / plan.weeks_total;
      nodes.push(el("div", { class: "progress-track", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": plan.weeks_total, "aria-valuenow": plan.weeks_done, "aria-label": "Plan weeks completed" },
        el("div", { class: "progress-fill", style: `width:${(share * 100).toFixed(1)}%` })));
    }
    nodes.push(box,
      legend([{ kind: "key-rect", color: "var(--series-1)", label: "TSS done" }, { kind: "key-tick", label: "Weekly target" }]),
      tableView([{ label: "Week" }, { label: "Dates" }, { label: "Type" }, { label: "Key session" }, { label: "Target TSS", num: true }, { label: "Done TSS", num: true }, { label: "Target h", num: true }, { label: "Done h", num: true }, { label: "Compliance", num: true }],
        plan.weeks.map((w) => [w.name, `${fmtDate(w.start_date)}–${fmtDate(w.end_date)}`, humanize(w.type), humanize(w.key_session), fmt(w.target_tss), fmt(w.tss),
          isNum(w.target_minutes) ? fmt(w.target_minutes / 60, 1) : "—", isNum(w.minutes) ? fmt(w.minutes / 60, 1) : "—", pct(w.compliance_pct)]), "Show all plan weeks"));
    setBody("plan", nodes);
    const items = plan.weeks.map((w, i) => ({
      value: w.tss, target: w.target_tss, partial: w.state === "current", highlight: i === plan.current_index,
      tick: `W${i + 1}`,
      title: `${w.name} · ${fmtDate(w.start_date)}–${fmtDate(w.end_date)}`,
      aria: `${w.name}: target ${fmt(w.target_tss)} TSS, done ${fmt(w.tss)}`,
      rows: [
        { value: `${fmt(w.target_tss)} TSS`, label: "target" },
        { value: w.state === "future" ? "upcoming" : `${fmt(w.tss)} TSS`, label: "done" },
        { value: humanize(w.type), label: `key: ${humanize(w.key_session)}` },
      ],
      note: isNum(w.compliance_pct) ? `compliance ${pct(w.compliance_pct)}` : w.state === "current" ? "current week" : null,
    }));
    const draw = () => columnChart(box, { items, label: "Plan weeks: target and done TSS", height: 200 });
    draw();
    resizers.set("plan", draw);
  }

  function renderAssessments(a) {
    if (!a.assessments.length) { setBody("assessments", empty("No AI assessments yet.")); return; }
    setBody("assessments", a.assessments.map((x) => {
      if (x.type === "daily_plan") {
        const outcomeCls = { planned: "s-good", rest: "s-neutral", fallback: "s-warning", aborted: "s-critical" }[x.outcome];
        return el("article", { class: "assessment" },
          el("div", { class: "assessment-head" }, el("strong", { text: `Daily plan · ${fmtDay(x.date)}` }), badge(humanize(x.outcome), outcomeCls),
            x.session_type ? el("span", { class: "muted", text: `${humanize(x.session_type)} · limit ${humanize(x.effective_cap)}` }) : null),
          x.goal ? el("p", { class: "prose", text: x.goal }) : null,
          x.rationale ? el("p", { class: "prose", text: x.rationale }) : null,
          x.fallback_reason ? el("p", { class: "prose" }, el("strong", { text: "Fallback: " }), x.fallback_reason) : null,
          x.abort_reason ? el("p", { class: "prose" }, el("strong", { text: "Aborted: " }), x.abort_reason) : null,
          el("details", {}, el("summary", { text: "Training and recovery assessment" }),
            x.training_summary ? el("p", { class: "prose" }, el("strong", { text: "Training: " }), x.training_summary) : null,
            x.recovery_summary ? el("p", { class: "prose" }, el("strong", { text: `Recovery (${humanize(x.recovery_state)}): ` }), x.recovery_summary) : null,
            x.concerns.length ? el("ul", { class: "plain prose" }, x.concerns.map((c) => el("li", { text: c }))) : null,
            x.long_term_link ? el("p", { class: "prose" }, el("strong", { text: "Long-term link: " }), x.long_term_link) : null,
            el("p", { class: "small muted", style: "margin-top:6px", text: `Model ${x.model || "—"} · ${x.llm_calls ?? 0} LLM calls · ${Object.values(x.prompt_versions).join(", ")} · ${fmtDateTime(x.created_at)}` })));
      }
      return el("article", { class: "assessment" },
        el("div", { class: "assessment-head" }, el("strong", { text: `Ride review · ${fmtDay(x.date)}` }),
          x.recommendation ? badge(humanize(x.recommendation), RECOMMENDATION[x.recommendation]) : badge("no review", "s-neutral"),
          x.outcome ? outcomeBadge(x.outcome) : null, x.activity_name ? el("span", { class: "muted", text: x.activity_name }) : null),
        x.summary ? el("p", { class: "prose", text: x.summary }) : null,
        x.went_well.length || x.concerns.length || x.next_session_note ? el("details", {}, el("summary", { text: "Details" }),
          x.went_well.length ? el("p", { class: "prose" }, el("strong", { text: "Went well: " }), x.went_well.join("; ")) : null,
          x.concerns.length ? el("p", { class: "prose" }, el("strong", { text: "Concerns: " }), x.concerns.join("; ")) : null,
          x.next_session_note ? el("p", { class: "prose" }, el("strong", { text: "Next session: " }), x.next_session_note) : null,
          el("p", { class: "small muted", text: `Model ${x.model || "—"} · ${x.prompt_version || ""}` })) : null);
    }));
  }

  // ------------------------------------------------------------------ wiring

  const resizers = new Map();
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { hideTip(); resizers.forEach((fn) => fn()); }, 150);
  });

  function loadRanged() {
    const d = state.days;
    load(["fitness", "form"], `/fitness?days=${d}`, renderFitness);
    load(["weekly"], `/weekly?weeks=${Math.max(1, Math.round(d / 7))}`, renderWeekly);
    load(["zones"], `/zones?days=${d}`, renderZones);
    load(["workouts"], `/workouts?days=${d}`, renderWorkouts);
  }

  for (const btn of document.querySelectorAll("button.range")) {
    btn.addEventListener("click", () => {
      document.querySelectorAll("button.range").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      state.days = Number(btn.dataset.days);
      try { localStorage.setItem("cycledays.range", String(state.days)); } catch (_) { /* storage unavailable */ }
      loadRanged();
    });
  }
  try {
    const saved = Number(localStorage.getItem("cycledays.range"));
    const btn = document.querySelector(`button.range[data-days="${saved}"]`);
    if (btn) {
      state.days = saved;
      document.querySelectorAll("button.range").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    }
  } catch (_) { /* storage unavailable */ }

  // Session expired or signed out elsewhere: back to the login page (relative,
  // so it works under a reverse-proxy prefix).
  let leaving = false;
  function toLogin() {
    if (!leaving) { leaving = true; window.location.assign("login"); }
  }

  fetch("api/session", { headers: { Accept: "application/json" }, cache: "no-store" })
    .then((res) => {
      if (res.status === 401) { toLogin(); return null; }
      return res.ok ? res.json() : null;
    })
    .then((s) => {
      const who = document.getElementById("signed-in-as");
      const avatar = document.getElementById("avatar");
      if (s && who) who.textContent = s.username;
      if (s && avatar && s.username) avatar.textContent = s.username.charAt(0).toUpperCase();
    })
    .catch(() => { /* header stays without a name */ });

  load(["next-workout", "readiness", "profile", "sync"], "/summary", renderSummary);
  load(["calendar"], "/calendar", renderCalendar);
  load(["plan"], "/plan", renderPlan);
  load(["assessments"], "/assessments?limit=10", renderAssessments);
  loadRanged();
})();
