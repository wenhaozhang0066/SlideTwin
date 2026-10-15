import assert from "node:assert/strict";
import { test } from "node:test";

import * as rep from "../../slidetwin/web/report.js";

const SCHEMA = {
  $defs: {
    Conclusion: {
      properties: {
        metastasis: { enum: ["present", "absent", "indeterminate"] },
        category: { enum: ["none", "itc", "micro", "macro", "indeterminate"] },
        extranodal_extension: { enum: ["present", "absent", "not_assessed"] },
      },
    },
  },
};

test("conclusion options come from the served schema", () => {
  const opts = rep.conclusionOptions(SCHEMA);
  assert.deepEqual(opts.metastasis, ["present", "absent", "indeterminate"]);
  assert.deepEqual(opts.extranodal_extension, ["present", "absent", "not_assessed"]);
  assert.deepEqual(rep.conclusionOptions({}), { metastasis: [], category: [], extranodal_extension: [] });
});

test("claims carry their evidence, measurement or scope", () => {
  const views = ["v1"];
  const f = rep.findingClaim("c1", "tumour cells", views);
  views.push("v2");
  assert.deepEqual(f, { id: "c1", type: "finding", text: "tumour cells", evidence: ["v1"] });
  assert.deepEqual(rep.measurementClaim("c2", "", "m3"), {
    id: "c2",
    type: "measurement",
    text: "",
    measurement_id: "m3",
  });
  assert.deepEqual(rep.negativeClaim("c3", "", null), { id: "c3", type: "negative", text: "" });
  assert.equal(rep.negativeClaim("c4", "", 2).scope, "tissue_piece:2");
  assert.equal(rep.negativeClaim("c5", "", 0).scope, "tissue_piece:0");
});

test("report copies claims and converts the size", () => {
  const claims = [rep.findingClaim("c1", "x", ["v1"])];
  const values = { metastasis: "present", category: "micro", largest_deposit_mm: "1.5", extranodal_extension: "absent" };
  const r = rep.buildReport("sln_metastasis", values, claims, undefined);
  assert.equal(r.conclusion.largest_deposit_mm, 1.5);
  assert.equal(r.free_text, "");
  r.claims[0].text = "changed";
  assert.equal(claims[0].text, "x");
  for (const empty of ["", null, undefined]) {
    const v = { ...values, largest_deposit_mm: empty };
    assert.equal(rep.buildReport("t", v, [], "").conclusion.largest_deposit_mm, null);
  }
});

test("claim ids fill the first gap", () => {
  assert.equal(rep.nextClaimId([]), "c1");
  assert.equal(rep.nextClaimId([{ id: "c1" }, { id: "c3" }]), "c2");
});
