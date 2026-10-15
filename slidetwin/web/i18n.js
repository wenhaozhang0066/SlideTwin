// UI strings in English and Chinese (SPEC 10.1). Plain sentences, explicit verbs (SPEC 10.2).

const STRINGS = {
  en: {
    "banner.research": "Research prototype. Not for clinical diagnosis.",
    "start.title": "Start a reading session",
    "start.intro":
      "Every action is logged in micrometres and seconds, and the log can be downloaded at the end. Use a pseudonym, not your name.",
    "start.slide": "Slide",
    "start.profile": "Rules",
    "start.protocol": "Protocol",
    "start.alias": "Pseudonym",
    "start.alias_hint": "1-64 letters, digits, dots, dashes or underscores.",
    "start.timer": "Show the time used",
    "start.button": "Start reading",
    "start.loading": "Loading the slide...",
    "reader.mag": "Magnification",
    "reader.tools": "Tools",
    "reader.overview": "Overview",
    "reader.overview_hint":
      "Click the overview to move there, or focus it, move the cursor with the arrow keys and press Enter.",
    "reader.keys":
      "Keys: 1-6 magnification; W/A/S/D pan (the arrow keys too while navigating); N navigate, M measure, P point, G polygon; Esc cancels.",
    "reader.time": "Time used: {s} s",
    "reader.view": "View {id} at {mag} ({mpp} µm/px)",
    "reader.unavailable": "Not available on this slide",
    "tool.navigate": "Navigate",
    "tool.measure": "Measure",
    "tool.point": "Point",
    "tool.polygon": "Polygon",
    "tool.measure_hint":
      "Click two points in the view, or move the cursor with the arrow keys (Shift for larger steps) and press Space at each point.",
    "tool.point_hint": "Click a point in the view, or move the cursor with the arrow keys and press Space.",
    "tool.polygon_hint":
      "Click or press Space to add corners; press Enter to finish or Esc to cancel. The arrow keys move the cursor.",
    "measure.result": "Measured {len} ({id}).",
    "annotate.label": "Label for this annotation",
    "annotate.saved": "Annotation {id} saved.",
    "report.title": "Report",
    "report.metastasis": "Metastasis",
    "report.category": "Category",
    "report.largest": "Largest deposit (mm)",
    "report.ene": "Extranodal extension",
    "report.claims": "Claims",
    "report.choose": "Choose...",
    "report.add_finding": "Add a finding from the current view",
    "report.add_measurement": "Add the last measurement",
    "report.add_negative": "Add a negative statement",
    "report.claim_text": "Statement",
    "report.scope": "Scope",
    "report.scope_slide": "Whole slide",
    "report.scope_piece": "Tissue piece {n}",
    "report.evidence": "Evidence: {ids}",
    "report.measurement": "Measurement: {id}",
    "report.remove": "Remove",
    "report.free_text": "Free text",
    "report.submit": "Submit report",
    "report.need_measurement": "Measure something first.",
    "receipt.title": "Report accepted",
    "receipt.refused": "Report refused",
    "receipt.coverage": "Tissue seen at {mag} or higher: {pct}",
    "receipt.violations": "{n} rule check(s) failed.",
    "receipt.closed": "The session is closed. Download the log below.",
    "status.pass": "passed",
    "status.fail": "failed",
    "status.not_applicable": "not applicable",
    "status.not_verifiable": "cannot be checked",
    "download": "Download trajectory",
    "lang.switch": "中文",
    "error.title": "That did not work:",
    "error.network": "The server could not be reached. Check the connection and try again.",
  },
  zh: {
    "banner.research": "研究原型，不用于临床诊断。",
    "start.title": "开始阅片",
    "start.intro":
      "每个操作都以微米和秒记录，结束时可以下载日志。请用化名，不要用真实姓名。",
    "start.slide": "切片",
    "start.profile": "规则",
    "start.protocol": "协议",
    "start.alias": "化名",
    "start.alias_hint": "1–64 个字母、数字、点、连字符或下划线。",
    "start.timer": "显示已用时间",
    "start.button": "开始阅片",
    "start.loading": "正在打开切片…",
    "reader.mag": "倍率",
    "reader.tools": "工具",
    "reader.overview": "小图",
    "reader.overview_hint": "点击小图跳转到该位置；也可以聚焦小图，用方向键移动光标，按 Enter 跳转。",
    "reader.keys":
      "按键：1–6 切换倍率；W/A/S/D 平移（浏览模式下方向键也可平移）；N 浏览，M 测量，P 点标注，G 多边形；Esc 取消。",
    "reader.time": "已用时间：{s} 秒",
    "reader.view": "视图 {id}，{mag}（{mpp} µm/px）",
    "reader.unavailable": "该切片不提供此倍率",
    "tool.navigate": "浏览",
    "tool.measure": "测量",
    "tool.point": "点",
    "tool.polygon": "多边形",
    "tool.measure_hint": "在视图中点击两个点；也可以用方向键移动光标（按住 Shift 步长更大），在每个点按空格。",
    "tool.point_hint": "在视图中点击一个点；也可以用方向键移动光标后按空格。",
    "tool.polygon_hint": "点击或按空格添加顶点；按 Enter 完成，按 Esc 取消。方向键移动光标。",
    "measure.result": "测得 {len}（{id}）。",
    "annotate.label": "标注的标签",
    "annotate.saved": "已保存标注 {id}。",
    "report.title": "报告",
    "report.metastasis": "转移",
    "report.category": "类别",
    "report.largest": "最大转移灶（mm）",
    "report.ene": "结外侵犯",
    "report.claims": "陈述",
    "report.choose": "请选择…",
    "report.add_finding": "用当前视图添加一条发现",
    "report.add_measurement": "添加最近一次测量",
    "report.add_negative": "添加一条阴性陈述",
    "report.claim_text": "陈述",
    "report.scope": "范围",
    "report.scope_slide": "整张切片",
    "report.scope_piece": "组织块 {n}",
    "report.evidence": "证据：{ids}",
    "report.measurement": "测量：{id}",
    "report.remove": "删除",
    "report.free_text": "自由文本",
    "report.submit": "提交报告",
    "report.need_measurement": "请先测量。",
    "receipt.title": "报告已接收",
    "receipt.refused": "报告被拒收",
    "receipt.coverage": "在 {mag} 及以上看过的组织：{pct}",
    "receipt.violations": "{n} 项规则检查未通过。",
    "receipt.closed": "会话已结束。请在下方下载日志。",
    "status.pass": "通过",
    "status.fail": "未通过",
    "status.not_applicable": "不适用",
    "status.not_verifiable": "无法检查",
    "download": "下载轨迹",
    "lang.switch": "English",
    "error.title": "操作未成功：",
    "error.network": "无法连接服务器，请检查网络后重试。",
  },
};

let lang = "en";

export const LANGS = Object.keys(STRINGS);

export function detectLang(navigatorLanguage) {
  return navigatorLanguage && navigatorLanguage.toLowerCase().startsWith("zh") ? "zh" : "en";
}

export function setLang(next) {
  lang = STRINGS[next] ? next : "en";
  return lang;
}

export function currentLang() {
  return lang;
}

export function keysOf(which) {
  return Object.keys(STRINGS[which]);
}

/** Translate `key`, filling {name} placeholders; falls back to English, then to the key. */
export function t(key, vars = {}) {
  const text = STRINGS[lang][key] ?? STRINGS.en[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
}
