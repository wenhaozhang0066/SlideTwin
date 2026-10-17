// SlideTwin reading UI (SPEC 10). It uses only the public session API: every pixel shown is a view or
// thumbnail the environment served and logged for this session.
import { api, ApiError } from "./api.js";
import * as geo from "./geometry.js";
import { currentLang, setLang, t } from "./i18n.js";
import * as rep from "./report.js";

const $ = (id) => document.getElementById(id);

const S = {
  sid: null,
  slide: null, // slide info from create_session
  task: null, // protocol
  rules: null, // profile
  schema: null, // report JSON Schema
  state: null,
  view: null, // current view or crop observation
  viewImg: null,
  thumb: null,
  thumbImg: null,
  mapScale: 1, // overview CSS px per thumbnail px
  cssPerView: 1,
  dpr: window.devicePixelRatio || 1,
  tool: "navigate",
  points: [], // view px of the measurement or polygon in progress
  cursor: null, // keyboard cursor on the view, view px (SPEC 10.2: everything works from the keyboard)
  mapCursor: null, // keyboard cursor on the overview, CSS px
  pending: null, // { geometry, input } of an annotation waiting for its label
  lastMeasurement: null, // { id, p1_um, p2_um, label }
  annotations: [], // { coords_um, type, label }
  claims: [],
  busy: false,
  closed: false,
  showTimer: false,
  drag: null, // a pan by dragging, started with the primary button on the view
  press: false, // a tool click started on the view with the primary button
  stale: null, // a view the server served but the page could not show (press R to retry)
  receipt: null,
  lastMove: 0, // time of the last action started by a key (held keys repeat)
  version: "",
  catalogue: null, // slides with their source and licence, for the start screen
};

const MOVES = new Set(["magnification", "pan", "move_to"]);

// --- messages and i18n ------------------------------------------------------------------------

function applyI18n() {
  document.documentElement.lang = currentLang();
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of document.querySelectorAll("[data-i18n-label]")) {
    el.setAttribute("aria-label", t(el.dataset.i18nLabel));
  }
  if (!S.sid && S.catalogue) showSlideSource();
  if (S.sid) {
    renderTools();
    renderReportForm();
    updateInfo();
    if (S.receipt) renderReceipt(S.receipt);
  }
}

let messageTimer = null;
function say(text, sticky = false) {
  const box = $("message");
  box.textContent = text;
  box.hidden = !text;
  clearTimeout(messageTimer);
  if (text && !sticky) messageTimer = setTimeout(() => (box.hidden = true), 6000);
}

function explain(err) {
  if (!(err instanceof ApiError)) return `${t("error.title")} ${err}`;
  if (err.code === "NETWORK") return t("error.network");
  return [t("error.title"), err.message, err.hint].filter(Boolean).join(" ");
}

// --- talking to the environment ------------------------------------------------------------

function clientMeta(input) {
  return {
    ui: "web",
    ui_version: S.version,
    input,
    display_scale: Math.round(geo.deviceScale(S.cssPerView, S.dpr) * 1e4) / 1e4,
    device_pixel_ratio: S.dpr,
  };
}

function setBusy(on) {
  S.busy = on;
  document.body.classList.toggle("busy", on);
}

/** Run one session action. Returns the response, or null if it failed (the error is shown). */
async function act(name, body, input) {
  if (S.busy || (S.closed && name !== "report")) return null;
  setBusy(true);
  try {
    let res = await api.action(S.sid, name, { ...body, client: clientMeta(input) });
    S.state = res.state;
    if (MOVES.has(name) && !S.rules.auto_view) {
      // A profile without automatic views: the page asks for the view a reader expects to see.
      res = await api.action(S.sid, "view", { client: clientMeta("auto") });
      S.state = res.state;
    }
    const obs = res.observation;
    if (obs && (obs.type === "view" || obs.type === "crop")) await showView(obs);
    updateInfo();
    return res;
  } catch (err) {
    if (err instanceof ApiError && err.code === "RULE_VIOLATION") showRefusal(err);
    else say(explain(err));
    return null;
  } finally {
    setBusy(false);
  }
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`could not load ${url}`));
    img.src = url;
  });
}

// --- the view ----------------------------------------------------------------------------------

/** Show a served view. The image is fetched again after a failure; if it still cannot be shown,
the page shows no view (tools then refuse to act on a view the reader never saw) until R reloads
it. A pending annotation label is dropped: its points belong to the previous view. */
async function showView(obs) {
  let img = null;
  for (const wait of [0, 400, 1200]) {
    if (wait) await new Promise((r) => setTimeout(r, wait));
    try {
      img = await loadImage(obs.image.url);
      break;
    } catch {
      img = null;
    }
  }
  if (S.pending) {
    cancelLabel();
    say(t("annotate.cancelled"));
  }
  S.points = [];
  S.cursor = null;
  if (!img) {
    S.view = null;
    S.stale = obs;
    const canvas = $("view-canvas");
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    $("overlay-canvas").getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    say(t("view.load_failed", { id: obs.view_id }), true);
    return;
  }
  S.viewImg = img;
  S.view = obs;
  S.stale = null;
  layoutStage();
  draw();
}

async function retryView() {
  if (!S.stale || S.busy) return;
  setBusy(true);
  try {
    await showView(S.stale);
    if (S.view) say("");
    updateInfo();
  } finally {
    setBusy(false);
  }
}

/** The view on screen is the session's current view (measure and annotate act on that one). */
function viewIsCurrent() {
  if (S.view && S.state && S.view.view_id === S.state.current_view_id) return true;
  say(t("view.not_current"));
  return false;
}

function layoutStage() {
  if (!S.view) return;
  const [w, h] = S.view.size_px;
  const stage = $("stage");
  S.dpr = window.devicePixelRatio || 1;
  S.cssPerView = geo.displayScale(w, h, stage.clientWidth - 16, stage.clientHeight - 16, S.dpr);
  for (const c of [$("view-canvas"), $("overlay-canvas")]) {
    c.width = w;
    c.height = h;
    c.style.width = `${w * S.cssPerView}px`;
    c.style.height = `${h * S.cssPerView}px`;
  }
}

function draw() {
  if (!S.view) return;
  const ctx = $("view-canvas").getContext("2d");
  ctx.drawImage(S.viewImg, 0, 0);
  drawOverlay();
  drawScaleBar();
  drawMinimap();
}

function accent() {
  return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#19c3d6";
}

/** Text with a dark halo, readable on pink and purple tissue as well as on glass. */
function haloText(ctx, text, x, y, lw) {
  ctx.save();
  ctx.lineWidth = 3 * lw;
  ctx.lineJoin = "round";
  ctx.strokeStyle = "rgba(0, 0, 0, 0.8)";
  ctx.strokeText(text, x, y);
  ctx.restore();
  ctx.fillText(text, x, y);
}

function drawOverlay() {
  const canvas = $("overlay-canvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const { bbox_um: bbox, mpp } = S.view;
  const lw = Math.max(1, 2 / S.cssPerView);
  ctx.lineWidth = lw;
  ctx.strokeStyle = accent();
  ctx.fillStyle = accent();
  ctx.font = `${Math.round(13 / S.cssPerView)}px ${getComputedStyle(document.body).fontFamily}`;
  const toView = ([x, y]) => geo.umToView(x, y, bbox, mpp);
  for (const a of S.annotations) {
    const pts = a.coords_um.map(toView);
    ctx.beginPath();
    if (a.type === "point") {
      ctx.arc(pts[0][0], pts[0][1], 6 * lw, 0, 2 * Math.PI);
    } else {
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.closePath();
    }
    ctx.stroke();
    haloText(ctx, a.label, pts[0][0] + 8 * lw, pts[0][1] - 8 * lw, lw);
  }
  if (S.lastMeasurement) {
    const [p, q] = [S.lastMeasurement.p1_um, S.lastMeasurement.p2_um].map(toView);
    ctx.beginPath();
    ctx.moveTo(p[0], p[1]);
    ctx.lineTo(q[0], q[1]);
    ctx.stroke();
    haloText(ctx, S.lastMeasurement.label, (p[0] + q[0]) / 2 + 6 * lw, (p[1] + q[1]) / 2 - 6 * lw, lw);
  }
  if (S.points.length) {
    ctx.beginPath();
    S.points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
    for (const [x, y] of S.points) ctx.fillRect(x - 3 * lw, y - 3 * lw, 6 * lw, 6 * lw);
  }
  if (S.cursor && S.tool !== "navigate") crosshair(ctx, S.cursor, 10 * lw);
}

function crosshair(ctx, [x, y], r) {
  ctx.beginPath();
  ctx.moveTo(x - r, y);
  ctx.lineTo(x + r, y);
  ctx.moveTo(x, y - r);
  ctx.lineTo(x, y + r);
  ctx.stroke();
}

function drawScaleBar() {
  const bar = geo.scaleBar(S.view.mpp, S.cssPerView);
  const box = $("scale-bar");
  box.querySelector(".bar").style.width = `${bar.css}px`;
  box.querySelector("span").textContent = bar.label;
}

// --- overview ------------------------------------------------------------------------------------

function drawMinimap() {
  const canvas = $("minimap");
  if (!S.thumbImg || !S.state) return;
  const maxCss = canvas.parentElement.clientWidth || 300;
  S.mapScale = Math.min(maxCss / S.thumbImg.width, 260 / S.thumbImg.height);
  canvas.width = Math.round(S.thumbImg.width * S.mapScale * S.dpr);
  canvas.height = Math.round(S.thumbImg.height * S.mapScale * S.dpr);
  canvas.style.width = `${S.thumbImg.width * S.mapScale}px`;
  canvas.style.height = `${S.thumbImg.height * S.mapScale}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  ctx.drawImage(S.thumbImg, 0, 0, S.thumbImg.width * S.mapScale, S.thumbImg.height * S.mapScale);
  ctx.strokeStyle = accent();
  ctx.fillStyle = accent();
  ctx.lineWidth = 2;
  const [x, y, w, h] = geo.umBoxToMinimap(S.state.bbox_um, S.thumb.um_per_px, S.mapScale);
  ctx.strokeRect(x, y, Math.max(w, 2), Math.max(h, 2));
  ctx.font = "11px sans-serif";
  const scope = $("negative-scope").value;
  for (const p of S.thumb.tissue_pieces) {
    const [px, py, pw, ph] = geo.umBoxToMinimap(p.bbox_um, S.thumb.um_per_px, S.mapScale);
    if (String(p.piece) === scope) {
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(px, py, pw, ph);
      ctx.setLineDash([]);
    }
    if (pw >= 14 && ph >= 12) ctx.fillText(String(p.piece), px + 2, py + 11);
  }
  if (S.mapCursor && document.activeElement === canvas) crosshair(ctx, S.mapCursor, 7);
}

function minimapSize() {
  return [S.thumbImg.width * S.mapScale, S.thumbImg.height * S.mapScale];
}

/** Centre of the current viewport on the overview, CSS px. */
function minimapCentre() {
  const [x, y, w, h] = geo.umBoxToMinimap(S.state.bbox_um, S.thumb.um_per_px, S.mapScale);
  return geo.clampPoint(x + w / 2, y + h / 2, ...minimapSize());
}

async function minimapMove(x, y, input) {
  const [ux, uy] = geo.minimapToUm(x, y, S.thumb.um_per_px, S.mapScale);
  await act("move_to", { x_um: ux, y_um: uy }, input);
}

async function onMinimapKey(ev) {
  if (!S.thumb || S.closed || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const step = geo.cursorStep(ev.key, ev.shiftKey, 4, 20);
  if (step) {
    ev.preventDefault();
    ev.stopPropagation(); // arrows move this cursor here; they do not pan
    const [x, y] = S.mapCursor ?? minimapCentre();
    S.mapCursor = geo.clampPoint(x + step[0], y + step[1], ...minimapSize());
    drawMinimap();
  } else if (ev.key === "Enter" || ev.key === " ") {
    ev.preventDefault();
    ev.stopPropagation();
    const [x, y] = S.mapCursor ?? minimapCentre();
    S.mapCursor = null;
    await minimapMove(x, y, "minimap-key");
  }
}

async function loadThumbnail() {
  if (!S.rules.thumbnail.allowed) return;
  const res = await act("thumbnail", {}, "auto");
  if (!res) return;
  S.thumb = res.observation;
  S.thumbImg = await loadImage(S.thumb.image.url);
  drawMinimap();
  renderScopeOptions();
}

// --- controls ------------------------------------------------------------------------------------

function renderMagButtons() {
  const box = $("mag-buttons");
  const focused = document.activeElement?.dataset?.mag;
  box.replaceChildren();
  const avail = new Set(S.slide.available_mags);
  for (const m of S.rules.magnifications) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = `${m}x`;
    b.dataset.mag = String(m);
    b.disabled = !avail.has(m) || S.closed;
    if (!avail.has(m)) b.title = t("reader.unavailable");
    b.setAttribute("aria-pressed", String(S.state && S.state.mag === m));
    b.addEventListener("click", () => setMag(m, "button"));
    box.append(b);
    if (focused === b.dataset.mag) b.focus();
  }
}

async function setMag(mag, input) {
  if (!S.slide.available_mags.includes(mag)) {
    say(t("reader.unavailable"));
    return;
  }
  await act("magnification", { mag }, input);
}

function renderTools() {
  for (const b of $("tool-buttons").querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(b.dataset.tool === S.tool));
  }
  const hints = { measure: "tool.measure_hint", point: "tool.point_hint", polygon: "tool.polygon_hint" };
  $("tool-hint").textContent = hints[S.tool] ? t(hints[S.tool]) : "";
  $("overlay-canvas").classList.toggle("tool", S.tool !== "navigate");
}

function setTool(tool) {
  S.tool = tool;
  S.points = [];
  if (tool === "navigate") S.cursor = null;
  cancelLabel();
  renderTools();
  if (S.view) drawOverlay();
}

function updateInfo() {
  if (!S.state) return;
  renderMagButtons();
  if (S.view) {
    const info = t("reader.view", {
      id: S.view.view_id,
      mag: `${Number(S.view.mag.toPrecision(4))}x`,
      mpp: Number(S.view.mpp.toPrecision(3)),
    });
    $("view-info").textContent = info;
    $("view-canvas").setAttribute("aria-label", info);
  }
  const timer = $("timer");
  timer.hidden = !S.showTimer;
  timer.textContent = t("reader.time", { s: S.state.elapsed_s.toFixed(1) });
  drawMinimap();
}

// --- pointer input on the view ----------------------------------------------------------------

function viewPoint(ev) {
  const r = $("overlay-canvas").getBoundingClientRect();
  const [x, y] = geo.cssToView(ev.clientX - r.left, ev.clientY - r.top, S.cssPerView);
  return geo.clampPoint(x, y, ...S.view.size_px);
}

function onPointerDown(ev) {
  S.press = false;
  if (!S.view || S.busy || S.closed || ev.button !== 0) return;
  if (S.tool === "navigate") {
    S.drag = { x: ev.clientX, y: ev.clientY };
    ev.target.setPointerCapture(ev.pointerId);
  } else {
    S.press = true;
  }
}

function onPointerMove(ev) {
  if (!S.drag) return;
  const dx = ev.clientX - S.drag.x;
  const dy = ev.clientY - S.drag.y;
  $("canvas-box").style.transform = `translate(${dx}px, ${dy}px)`;
}

async function onPointerUp(ev) {
  if (S.drag) {
    const dx = ev.clientX - S.drag.x;
    const dy = ev.clientY - S.drag.y;
    S.drag = null;
    $("canvas-box").style.transform = "";
    if (Math.hypot(dx, dy) > 3) {
      const [w, h] = S.view.size_px;
      const [pdx, pdy] = geo.dragToPan(dx, dy, w * S.cssPerView, h * S.cssPerView);
      await act("pan", { dx: pdx, dy: pdy }, "drag");
    }
    return;
  }
  const pressed = S.press;
  S.press = false;
  if (!pressed || ev.button !== 0 || !S.view || S.busy || S.closed) return;
  await placePoint(viewPoint(ev), "click");
}

/** A point placed with the current tool, by a click or by Space at the keyboard cursor. */
async function placePoint(p, input) {
  if (S.tool === "measure") {
    S.points.push(p);
    drawOverlay();
    if (S.points.length === 2) await measure(input);
  } else if (S.tool === "point") {
    askLabel({ type: "point", coordinates: p }, input);
  } else if (S.tool === "polygon") {
    S.points.push(p);
    drawOverlay();
  }
}

function moveCursor([dx, dy]) {
  const [w, h] = S.view.size_px;
  const [x, y] = S.cursor ?? [w / 2, h / 2];
  S.cursor = geo.clampPoint(x + dx, y + dy, w, h);
  drawOverlay();
}

async function measure(input) {
  const [p1, p2] = S.points;
  S.points = [];
  if (!viewIsCurrent()) return drawOverlay();
  const res = await act("measure", { p1_px: p1, p2_px: p2 }, input);
  if (!res) return drawOverlay();
  const m = res.observation;
  const label = geo.formatLength(m.length_um);
  S.lastMeasurement = { id: m.measurement_id, p1_um: m.p1_um, p2_um: m.p2_um, label };
  drawOverlay();
  say(t("measure.result", { len: label, id: m.measurement_id }));
}

function askLabel(geometry, input) {
  S.pending = { geometry, input };
  $("label-form").hidden = false;
  $("label-input").value = "";
  $("label-input").focus();
}

function cancelLabel() {
  S.pending = null;
  $("label-form").hidden = true;
}

async function saveAnnotation(ev) {
  ev.preventDefault();
  const label = $("label-input").value.trim();
  if (!S.pending || !label) return;
  if (!viewIsCurrent()) return cancelLabel();
  const { geometry, input } = S.pending;
  cancelLabel();
  S.points = [];
  const res = await act("annotations", { geometry, label }, input);
  if (res) {
    const a = res.observation;
    S.annotations.push({ type: a.geometry_type, coords_um: a.coords_um, label: a.label });
    say(t("annotate.saved", { id: a.annotation_id }));
  }
  drawOverlay();
  $("overlay-canvas").focus();
}

// --- keyboard ----------------------------------------------------------------------------------

async function onKey(ev) {
  if (!S.sid || S.closed || ev.ctrlKey || ev.metaKey || ev.altKey || $("help").open) return;
  const target = ev.target;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)) {
    if (ev.key === "Escape") cancelLabel();
    return;
  }
  if (ev.key === "Escape") {
    setTool(S.tool);
    return;
  }
  // With a tool, the arrow keys move a cursor on the view and Space places a point there.
  const onView = target === $("overlay-canvas") || target === document.body;
  if (S.tool !== "navigate" && onView && S.view) {
    const step = geo.cursorStep(ev.key, ev.shiftKey, 10, 50);
    if (step) {
      ev.preventDefault();
      return moveCursor(step);
    }
    if (ev.key === " ") {
      ev.preventDefault();
      const [w, h] = S.view.size_px;
      return placePoint(S.cursor ?? [w / 2, h / 2], "key");
    }
  }
  if (ev.key === "Enter" && S.tool === "polygon" && S.points.length >= 3) {
    ev.preventDefault();
    askLabel({ type: "polygon", coordinates: S.points.slice() }, "key");
    return;
  }
  const tools = { n: "navigate", m: "measure", p: "point", g: "polygon" };
  const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  if (tools[key]) return setTool(tools[key]);
  if (key === "r") return retryView();
  // A held key repeats; let it move at most about three times a second.
  if (ev.repeat && performance.now() - S.lastMove < 300) {
    ev.preventDefault();
    return;
  }
  const mag = geo.presetForKey(ev.key, S.rules.magnifications);
  if (mag !== null) {
    ev.preventDefault();
    S.lastMove = performance.now();
    return setMag(mag, "key");
  }
  const pan = geo.panForKey(ev.key);
  if (pan) {
    ev.preventDefault();
    S.lastMove = performance.now();
    await act("pan", { dx: pan[0], dy: pan[1] }, "key");
  }
}

// --- report ----------------------------------------------------------------------------------

/** Options with an empty first choice, so that no answer is preselected; keeps the current value. */
function fillSelect(select, options) {
  const keep = select.value;
  select.replaceChildren(
    option("", t("report.choose")),
    ...options.map((o) => (typeof o === "string" ? option(o, o) : option(o.value, o.text))),
  );
  if ([...select.options].some((o) => o.value === keep)) select.value = keep;
}

function renderReportForm() {
  const opts = rep.conclusionOptions(S.schema);
  fillSelect($("r-metastasis"), opts.metastasis);
  fillSelect($("r-category"), opts.category);
  fillSelect($("r-ene"), opts.extranodal_extension);
  renderScopeOptions();
  renderClaims();
}

function renderScopeOptions() {
  const select = $("negative-scope");
  const keep = select.value;
  const pieces = S.thumb ? S.thumb.tissue_pieces : [];
  select.replaceChildren(
    option("", t("report.scope_slide")),
    ...pieces.map((p) => option(String(p.piece), t("report.scope_piece", { n: p.piece }))),
  );
  if ([...select.options].some((o) => o.value === keep)) select.value = keep;
  select.setAttribute("aria-label", t("report.scope"));
}

function renderClaims() {
  const list = $("claims");
  list.replaceChildren();
  for (const claim of S.claims) {
    const li = document.createElement("li");
    const head = document.createElement("div");
    head.className = "claim-head";
    let detail = "";
    if (claim.type === "finding") detail = t("report.evidence", { ids: claim.evidence.join(", ") });
    if (claim.type === "measurement") detail = t("report.measurement", { id: claim.measurement_id });
    if (claim.type === "negative") {
      detail = claim.scope ? t("report.scope_piece", { n: claim.scope.split(":")[1] }) : t("report.scope_slide");
    }
    head.textContent = `${claim.id} · ${claim.type} · ${detail}`;
    const remove = Object.assign(document.createElement("button"), {
      type: "button",
      textContent: t("report.remove"),
      disabled: S.closed,
    });
    remove.addEventListener("click", () => {
      S.claims = S.claims.filter((c) => c !== claim);
      renderClaims();
      $("add-finding").focus();
    });
    head.append(remove);
    const text = Object.assign(document.createElement("input"), {
      value: claim.text,
      maxLength: 2000,
      required: true,
      placeholder: t("report.claim_text"),
      disabled: S.closed,
    });
    text.setAttribute("aria-label", t("report.claim_text"));
    text.addEventListener("input", () => (claim.text = text.value));
    li.append(head, text);
    list.append(li);
  }
}

function addClaim(claim) {
  S.claims.push(claim);
  renderClaims();
}

function readValues() {
  return {
    metastasis: $("r-metastasis").value,
    category: $("r-category").value,
    largest_deposit_mm: $("r-largest").value,
    extranodal_extension: $("r-ene").value,
  };
}

async function submitReport(ev) {
  ev.preventDefault();
  const report = rep.buildReport(S.task.protocol_id, readValues(), S.claims, $("r-free").value);
  const res = await act("report", { report }, "button");
  if (!res) return;
  showReceipt(res.observation);
}

function checksList(checks) {
  const ul = document.createElement("ul");
  for (const c of checks) {
    const li = document.createElement("li");
    li.className = c.status === "fail" ? "fail" : "";
    li.textContent = `${c.rule} ${t(`status.${c.status}`)}: ${c.message}`;
    ul.append(li);
  }
  return ul;
}

function showReceipt(receipt) {
  S.closed = true;
  S.receipt = receipt;
  showFeedbackForm();
  renderReceipt(receipt);
  for (const el of document.querySelectorAll(
    "#report-form button, #report-form input, #report-form select, #report-form textarea, #mag-buttons button, #tool-buttons button",
  )) {
    el.disabled = true;
  }
}

function renderReceipt(receipt) {
  const box = $("receipt");
  const cov = receipt.coverage;
  box.replaceChildren(
    Object.assign(document.createElement("strong"), { textContent: t("receipt.title") }),
    Object.assign(document.createElement("p"), {
      textContent: t("receipt.coverage", { mag: `${cov.at_or_above_mag}x`, pct: `${(cov.fraction * 100).toFixed(1)}%` }),
    }),
    Object.assign(document.createElement("p"), { textContent: t("receipt.violations", { n: receipt.violations }) }),
    checksList(receipt.rule_checks),
    Object.assign(document.createElement("p"), { className: "muted", textContent: t("receipt.closed") }),
  );
  box.hidden = false;
}

// --- pilot feedback (optional, after the report) ---------------------------------------------------

function showFeedbackForm() {
  const form = $("feedback-form");
  for (const set of form.querySelectorAll("fieldset[data-question]")) {
    for (const old of set.querySelectorAll("label")) old.remove();
    for (let v = 1; v <= 5; v += 1) {
      const radio = Object.assign(document.createElement("input"), {
        type: "radio",
        name: set.dataset.question,
        value: String(v),
      });
      const label = document.createElement("label");
      label.className = "rating";
      label.append(radio, String(v));
      set.append(label);
    }
  }
  form.hidden = false;
}

async function sendFeedback(ev) {
  ev.preventDefault();
  const form = $("feedback-form");
  const body = { comment: $("f-comment").value };
  for (const set of form.querySelectorAll("fieldset[data-question]")) {
    const picked = set.querySelector("input:checked");
    if (picked) body[set.dataset.question] = Number(picked.value);
  }
  try {
    await api.feedback(S.sid, body);
    form.hidden = true;
    $("feedback-thanks").hidden = false;
  } catch (err) {
    say(explain(err));
  }
}

function showRefusal(err) {
  const box = $("receipt");
  box.replaceChildren(
    Object.assign(document.createElement("strong"), { textContent: t("receipt.refused") }),
    Object.assign(document.createElement("p"), { textContent: err.hint }),
    checksList(err.violations),
  );
  box.hidden = false;
}

// --- start ---------------------------------------------------------------------------------------

function option(value, text) {
  return Object.assign(document.createElement("option"), { value, textContent: text });
}

async function loadCatalogue() {
  try {
    const [slides, profiles, protocols, schema, health] = await Promise.all([
      api.slides(),
      api.profiles(),
      api.protocols(),
      api.reportSchema(),
      api.health(),
    ]);
    S.schema = schema;
    S.version = `${health.version}+${health.git_sha}`;
    S.catalogue = slides.slides;
    $("slide-select").replaceChildren(...slides.slides.map((s) => option(s.id, s.id)));
    showSlideSource();
    $("profile-select").replaceChildren(...profiles.profiles.map((p) => option(p.id, `${p.id} — ${p.description}`)));
    // The microscope-like rules by default, as for API sessions (the list is in alphabetical order).
    if (profiles.profiles.some((p) => p.id === "viewer")) $("profile-select").value = "viewer";
    $("protocol-select").replaceChildren(...protocols.protocols.map((p) => option(p.protocol_id, p.title)));
  } catch (err) {
    say(explain(err), true);
  }
}

/** Every slide is shown with its source and licence (DATA_LICENSES.md). */
function showSlideSource() {
  const slide = (S.catalogue || []).find((s) => s.id === $("slide-select").value);
  const unknown = t("start.unknown");
  $("slide-source").textContent = slide
    ? t("start.source", { attribution: slide.attribution || unknown, license: slide.license || unknown })
    : "";
}

async function start(ev) {
  ev.preventDefault();
  const alias = $("alias");
  if (!alias.checkValidity()) {
    alias.reportValidity();
    return;
  }
  $("start-button").disabled = true;
  $("start-status").textContent = t("start.loading");
  S.showTimer = $("timer-toggle").checked;
  try {
    const res = await api.createSession({
      slide_id: $("slide-select").value,
      profile: $("profile-select").value,
      protocol: $("protocol-select").value,
      actor: `human:${alias.value}`,
    });
    Object.assign(S, { sid: res.session_id, slide: res.slide, task: res.task, rules: res.rules, state: res.state });
    $("start").hidden = true;
    $("reader").hidden = false;
    const link = $("download");
    link.href = api.trajectoryUrl(S.sid);
    link.setAttribute("download", `${S.sid}.jsonl`);
    renderReportForm();
    renderTools();
    await showView(res.observation);
    updateInfo();
    await loadThumbnail();
    $("overlay-canvas").focus();
  } catch (err) {
    say(explain(err), true);
    $("start-button").disabled = false;
    $("start-status").textContent = "";
  }
}

// --- wiring ----------------------------------------------------------------------------------

function init() {
  setLang("en"); // the deployed UI starts in English for everyone; 中文 is one click away
  applyI18n();
  $("help-button").addEventListener("click", () => $("help").showModal());
  $("feedback-form").addEventListener("submit", sendFeedback);
  $("lang-switch").addEventListener("click", () => {
    setLang(currentLang() === "en" ? "zh" : "en");
    applyI18n();
  });
  $("start-form").addEventListener("submit", start);
  $("slide-select").addEventListener("change", showSlideSource);
  const overlay = $("overlay-canvas");
  overlay.addEventListener("pointerdown", onPointerDown);
  overlay.addEventListener("pointermove", onPointerMove);
  overlay.addEventListener("pointerup", onPointerUp);
  const minimap = $("minimap");
  minimap.addEventListener("click", async (ev) => {
    if (!S.thumb || S.closed) return;
    const r = minimap.getBoundingClientRect();
    S.mapCursor = null;
    await minimapMove(ev.clientX - r.left, ev.clientY - r.top, "minimap");
  });
  minimap.addEventListener("keydown", onMinimapKey);
  minimap.addEventListener("focus", () => {
    if (!S.thumb) return;
    S.mapCursor = minimapCentre();
    drawMinimap();
  });
  minimap.addEventListener("blur", () => {
    S.mapCursor = null;
    drawMinimap();
  });
  for (const b of $("tool-buttons").querySelectorAll("button")) {
    b.addEventListener("click", () => {
      setTool(b.dataset.tool);
      $("overlay-canvas").focus(); // the arrow keys and Space then drive the tool
    });
  }
  overlay.addEventListener("contextmenu", (ev) => ev.preventDefault());
  $("label-form").addEventListener("submit", saveAnnotation);
  $("add-finding").addEventListener("click", () => {
    if (S.view) addClaim(rep.findingClaim(rep.nextClaimId(S.claims), "", [S.view.view_id]));
  });
  $("add-measurement").addEventListener("click", () => {
    if (!S.lastMeasurement) return say(t("report.need_measurement"));
    addClaim(rep.measurementClaim(rep.nextClaimId(S.claims), "", S.lastMeasurement.id));
  });
  $("add-negative").addEventListener("click", () => {
    const v = $("negative-scope").value;
    addClaim(rep.negativeClaim(rep.nextClaimId(S.claims), "", v === "" ? null : Number(v)));
  });
  $("report-form").addEventListener("submit", submitReport);
  // Enter in a field must not submit the report: submitting ends the session.
  $("report-form").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.tagName === "INPUT") ev.preventDefault();
  });
  $("negative-scope").addEventListener("change", drawMinimap);
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", () => {
    if (!S.view) return;
    layoutStage();
    draw();
  });
  loadCatalogue();
}

init();
