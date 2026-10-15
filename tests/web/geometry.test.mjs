// Unit tests for the reading UI's display geometry: `node --test tests/web/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import * as geo from "../../slidetwin/web/geometry.js";

const close = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} != ${b}`);

test("view is shown at one device pixel per view pixel when it fits", () => {
  close(geo.displayScale(1280, 1024, 1600, 1200, 1), 1);
  close(geo.displayScale(1280, 1024, 1600, 1200, 2), 0.5); // retina: half a CSS px per view px
  close(geo.deviceScale(geo.displayScale(1280, 1024, 1600, 1200, 2), 2), 1);
});

test("view shrinks to fit a small stage and never grows", () => {
  close(geo.displayScale(1280, 1024, 640, 1024, 1), 0.5);
  close(geo.displayScale(1280, 1024, 4000, 4000, 1), 1);
  assert.ok(geo.displayScale(1280, 1024, 1, 1, 1) >= 0.05);
});

test("css, view and micrometre conversions round-trip", () => {
  const bbox = [1000, 2000, 1000 + 1280 * 0.5, 2000 + 1024 * 0.5];
  const [vx, vy] = geo.cssToView(320, 256, 0.5);
  assert.deepEqual([vx, vy], [640, 512]);
  assert.deepEqual(geo.viewToCss(vx, vy, 0.5), [320, 256]);
  const [ux, uy] = geo.viewToUm(vx, vy, bbox, 0.5);
  assert.deepEqual([ux, uy], [1320, 2256]);
  assert.deepEqual(geo.umToView(ux, uy, bbox, 0.5), [640, 512]);
});

test("dragging the image right moves the view left", () => {
  assert.deepEqual(geo.dragToPan(640, -256, 1280, 1024), [-0.5, 0.25]);
  const [dx, dy] = geo.dragToPan(0, 0, 1280, 1024);
  assert.ok(Object.is(dx, 0) && Object.is(dy, 0)); // no negative zero in the logged parameters
});

test("scale bar uses 1, 2 or 5 times a power of ten", () => {
  assert.equal(geo.niceLength(0), 0);
  assert.equal(geo.niceLength(-3), 0);
  assert.equal(geo.niceLength(7.3), 5);
  assert.equal(geo.niceLength(20), 20);
  assert.equal(geo.niceLength(199.9), 100);
  assert.equal(geo.niceLength(0.03), 0.02);
  const bar = geo.scaleBar(0.25, 1); // 40x at 1:1: 120 css px = 30 um
  assert.equal(bar.um, 20);
  close(bar.css, 80);
  assert.equal(bar.label, "20 µm");
  assert.equal(geo.scaleBar(4, 0.5).label, "500 µm");
  assert.equal(geo.scaleBar(16, 1).label, "1 mm");
});

test("lengths are formatted in um below a millimetre", () => {
  assert.equal(geo.formatLength(2.345), "2.35 µm");
  assert.equal(geo.formatLength(999), "999 µm");
  assert.equal(geo.formatLength(2500), "2.5 mm");
});

test("overview clicks map to slide micrometres and back", () => {
  const umPerPx = [40, 41];
  const scale = 0.5; // overview css px per thumbnail px
  assert.deepEqual(geo.minimapToUm(50, 100, umPerPx, scale), [4000, 8200]);
  assert.deepEqual(geo.umBoxToMinimap([4000, 8200, 8000, 16400], umPerPx, scale), [50, 100, 50, 100]);
});

test("keys map to half-viewport pans and presets", () => {
  assert.deepEqual(geo.panForKey("ArrowRight"), [0.5, 0]);
  assert.deepEqual(geo.panForKey("W"), [0, -0.5]);
  assert.deepEqual(geo.panForKey("a"), [-0.5, 0]);
  assert.equal(geo.panForKey("x"), null);
  const presets = [1.25, 2.5, 5, 10, 20, 40];
  assert.equal(geo.presetForKey("1", presets), 1.25);
  assert.equal(geo.presetForKey("6", presets), 40);
  assert.equal(geo.presetForKey("7", presets), null);
  assert.equal(geo.presetForKey("3", [5, 10]), null);
});

test("keyboard cursor steps and clamping", () => {
  assert.deepEqual(geo.cursorStep("ArrowLeft", false, 10, 50), [-10, 0]);
  assert.deepEqual(geo.cursorStep("ArrowDown", true, 10, 50), [0, 50]);
  const [dx, dy] = geo.cursorStep("ArrowUp", false, 4, 20);
  assert.ok(Object.is(dx, 0) && dy === -4);
  assert.equal(geo.cursorStep("w", false, 10, 50), null);
  assert.deepEqual(geo.clampPoint(-5, 2000, 1280, 1024), [0, 1024]);
  assert.deepEqual(geo.clampPoint(10.5, 20.25, 1280, 1024), [10.5, 20.25]);
});
