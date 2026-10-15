// The only way the reading UI talks to the server: the public session API (AGENTS.md, SPEC 6.3).
// There is no tile endpoint; images are those already served to this session.

export class ApiError extends Error {
  constructor(status, body) {
    super(body.message || `HTTP ${status}`);
    this.status = status;
    this.code = body.code || `HTTP_${status}`;
    this.hint = body.hint || "";
    this.violations = body.violations || [];
    this.details = body.details || [];
  }
}

async function call(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? {} : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, { code: "NETWORK", message: "" });
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.error || {});
  return data;
}

const session = (sid, tail) => `/api/sessions/${encodeURIComponent(sid)}/${tail}`;

export const api = {
  slides: () => call("GET", "/api/slides"),
  profiles: () => call("GET", "/api/profiles"),
  protocols: () => call("GET", "/api/protocols"),
  reportSchema: () => call("GET", "/api/report_schema"),
  createSession: (body) => call("POST", "/api/sessions?include_image=false", body),
  state: (sid) => call("GET", session(sid, "state")),
  /** Any POST action (thumbnail, magnification, pan, move_to, view, measure, annotations, report). */
  action: (sid, name, body) => call("POST", session(sid, `${name}?include_image=false`), body),
  trajectoryUrl: (sid) => session(sid, "trajectory"),
  /** Optional pilot feedback, stored next to the session log. */
  feedback: (sid, body) => call("POST", session(sid, "feedback"), body),
};
