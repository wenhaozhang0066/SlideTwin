// Report building (SPEC 5.3): pure functions shared by the form and the tests.

/** Enum options of the conclusion fields, read from the served report schema. */
export function conclusionOptions(schema) {
  const props = schema.$defs?.Conclusion?.properties ?? {};
  const pick = (name) => props[name]?.enum ?? [];
  return {
    metastasis: pick("metastasis"),
    category: pick("category"),
    extranodal_extension: pick("extranodal_extension"),
  };
}

export function findingClaim(id, text, viewIds) {
  return { id, type: "finding", text, evidence: [...viewIds] };
}

export function measurementClaim(id, text, measurementId) {
  return { id, type: "measurement", text, measurement_id: measurementId };
}

/** A negative statement; `piece` null means the whole slide. */
export function negativeClaim(id, text, piece) {
  const claim = { id, type: "negative", text };
  if (piece !== null && piece !== undefined) claim.scope = `tissue_piece:${piece}`;
  return claim;
}

/** The report object submitted to POST .../report. */
export function buildReport(task, values, claims, freeText) {
  const size = values.largest_deposit_mm;
  return {
    task,
    conclusion: {
      metastasis: values.metastasis,
      category: values.category,
      largest_deposit_mm: size === "" || size === null || size === undefined ? null : Number(size),
      extranodal_extension: values.extranodal_extension,
    },
    claims: claims.map((c) => ({ ...c })),
    free_text: freeText ?? "",
  };
}

/** Next unused claim id ("c1", "c2", ...). */
export function nextClaimId(claims) {
  const used = new Set(claims.map((c) => c.id));
  let n = 1;
  while (used.has(`c${n}`)) n += 1;
  return `c${n}`;
}
