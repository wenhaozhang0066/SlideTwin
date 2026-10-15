// Pure display geometry for the reading UI. No DOM access, so node's test runner can check it.
// View pixels are the environment's pixels; CSS pixels are what the browser lays out.

/** CSS px per view px: 1:1 device pixels when the view fits (SPEC 3, JSP 8.2), else shrink. */
export function displayScale(viewW, viewH, availW, availH, dpr) {
  const oneToOne = 1 / dpr;
  const fit = Math.min(availW / viewW, availH / viewH);
  return Math.max(0.05, Math.min(oneToOne, fit));
}

/** Device pixels per view pixel, logged with every action as the display scale. */
export function deviceScale(cssPerView, dpr) {
  return cssPerView * dpr;
}

export function cssToView(x, y, cssPerView) {
  return [x / cssPerView, y / cssPerView];
}

export function viewToCss(px, py, cssPerView) {
  return [px * cssPerView, py * cssPerView];
}

/** Micrometres of a point given in view pixels (pixel k spans [k, k+1)). */
export function viewToUm(px, py, bbox, mpp) {
  return [bbox[0] + px * mpp, bbox[1] + py * mpp];
}

export function umToView(x, y, bbox, mpp) {
  return [(x - bbox[0]) / mpp, (y - bbox[1]) / mpp];
}

/** A drag of the image by (dx, dy) CSS px moves the view the opposite way, in viewport units. */
export function dragToPan(dxCss, dyCss, displayW, displayH) {
  return [-dxCss / displayW + 0, -dyCss / displayH + 0];
}

/** Largest 1, 2 or 5 times a power of ten that does not exceed `um`. */
export function niceLength(um) {
  if (!(um > 0)) return 0;
  const base = 10 ** Math.floor(Math.log10(um));
  for (const m of [5, 2, 1]) {
    if (m * base <= um * (1 + 1e-9)) return m * base;
  }
  return base;
}

export function formatLength(um) {
  if (um >= 1000) return `${Number((um / 1000).toPrecision(3))} mm`;
  return `${Number(um.toPrecision(3))} µm`;
}

/** Scale bar of a round length about `targetCss` CSS px long. */
export function scaleBar(mppView, cssPerView, targetCss = 120) {
  const umPerCss = mppView / cssPerView;
  const um = niceLength(targetCss * umPerCss);
  return { um, css: um / umPerCss, label: formatLength(um) };
}

/** Overview (minimap) click in CSS px -> slide micrometres. */
export function minimapToUm(x, y, umPerThumbPx, minimapCssPerThumbPx) {
  return [
    (x / minimapCssPerThumbPx) * umPerThumbPx[0],
    (y / minimapCssPerThumbPx) * umPerThumbPx[1],
  ];
}

/** Slide micrometre box -> overview CSS box [x, y, w, h]. */
export function umBoxToMinimap(bbox, umPerThumbPx, minimapCssPerThumbPx) {
  const sx = minimapCssPerThumbPx / umPerThumbPx[0];
  const sy = minimapCssPerThumbPx / umPerThumbPx[1];
  return [bbox[0] * sx, bbox[1] * sy, (bbox[2] - bbox[0]) * sx, (bbox[3] - bbox[1]) * sy];
}

const PAN_KEYS = {
  ArrowUp: [0, -0.5],
  ArrowDown: [0, 0.5],
  ArrowLeft: [-0.5, 0],
  ArrowRight: [0.5, 0],
  w: [0, -0.5],
  s: [0, 0.5],
  a: [-0.5, 0],
  d: [0.5, 0],
};

/** Half-viewport pan for arrow and WASD keys (SPEC 10.1), or null. */
export function panForKey(key) {
  const k = key.length === 1 ? key.toLowerCase() : key;
  return PAN_KEYS[k] ?? null;
}

/** Preset bound to keys 1-6, in the profile's order, or null. */
export function presetForKey(key, presets) {
  const i = "123456".indexOf(key);
  return i >= 0 && i < presets.length ? presets[i] : null;
}

const CURSOR_KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

/** Arrow-key move of a keyboard cursor: `step` px, or `fastStep` with Shift; null for other keys. */
export function cursorStep(key, shift, step, fastStep) {
  const d = CURSOR_KEYS[key];
  if (!d) return null;
  const s = shift ? fastStep : step;
  return [d[0] * s + 0, d[1] * s + 0];
}

/** Clamp a point to [0, w] x [0, h]. */
export function clampPoint(x, y, w, h) {
  return [Math.min(Math.max(x, 0), w), Math.min(Math.max(y, 0), h)];
}
