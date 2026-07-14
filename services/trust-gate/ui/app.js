/* ===========================================================================
   Trust Gate QC  standalone frontend logic (no build step).
   Flow: submit (FHIR | OMOP | tabular) -> animated step-by-step QC pipeline ->
   full Quality Passport report + full ALCOA++ audit report.
   =========================================================================== */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (n) => Math.round(Number(n) || 0);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rateColor = (p) => (p >= 90 ? "var(--pass)" : p >= 80 ? "var(--cond)" : "var(--block)");
const rateText = (p) => (p >= 90 ? "ok" : p >= 80 ? "warn" : "bad");

// ---- humanizer: plain-language problems + where they are -------------------
// The passport is PHI-safe: a violation detail carries the record, the exact
// field/path, and a short reason  never the raw field value (a value can be
// PHI). We translate the technical signal and pinpoint the location.
const CHECK_PLAIN = {
  "conformance.structural": "The FHIR validator reported a structural error in this record.",
  "conformance.value_format": "A field is not in the format FHIR requires (e.g. a malformed id or date).",
  "conformance.reference_integrity": "A reference points to a record that is not present in the dataset.",
  "conformance.status_not_entered_in_error": "This record is marked entered-in-error and must be removed before processing.",
  "conformance.id_present": "The record has no id.",
  "conformance.terminology": "A code is not recognized in its declared code system.",
  "terminology.validity": "A code is not recognized in its declared code system.",
  "completeness.required_elements": "A required field is missing on this record.",
  "completeness.value_or_absent": "This Observation has neither a result value nor a stated reason it is absent.",
  "completeness.recommended_elements": "A recommended field is missing (not blocking, lowers richness).",
  "plausibility.value_outlier": "This measurement is an extreme outlier versus comparable measurements.",
  "plausibility.value_outlier_stratified": "This measurement is an outlier for the patient's age/sex group.",
  "plausibility.distribution_drift": "The value distribution shifted sharply from the established baseline.",
  "clinical.measurement_after_birth": "A measurement is dated before the patient was born.",
  "temporal.ordering": "Two dates are in an impossible order (e.g. an end before its start).",
  "temporal.plausibility": "A date falls outside a plausible window.",
  "identity.uniqueness": "A business identifier is duplicated across records.",
  "identity.integrity": "An identifier is missing or malformed.",
  "provenance.present": "The record carries no provenance (source / extraction) metadata.",
};
function whatItChecks(c) {
  return c.description || CHECK_PLAIN[c.check_id] || prettyCheck(c.check_id);
}
function prettyCheck(id) {
  if (CHECK_PLAIN[id]) return CHECK_PLAIN[id];
  const leaf = (id.split(".").pop() || id).replace(/_/g, " ");
  return leaf.charAt(0).toUpperCase() + leaf.slice(1);
}
function humanizeDetail(id, detail) {
  if (CHECK_PLAIN[id]) return CHECK_PLAIN[id];
  if (!detail) return prettyCheck(id);
  return String(detail)
    .replace(/^id absent or empty$/i, "The record has no id.")
    .replace(/dataAbsentReason/g, "reason-for-absence")
    .replace(/value\[x\]/g, "result value");
}
function locateProblem(id, d) {
  const rtype = d.resource_type ? String(d.resource_type) : "";
  const rid = d.resource_id != null && String(d.resource_id) !== "" ? String(d.resource_id)
    : d.resource_index != null ? `[${d.resource_index}]` : "";
  const resource = rtype ? `${rtype}${rid && !rid.startsWith("[") ? "/" : ""}${rid}` : (rid || "");
  const field = String(d.path ?? d.attribute ?? "");
  const raw = d.found ?? d.value ?? d.expected;
  const value = raw != null && raw !== "" ? String(raw) : null;
  return { resource, field, value, problem: humanizeDetail(id, d.detail) };
}
function affectedRecords(c) {
  const rows = c.violation_details || [];
  if (!rows.length) return "";
  const body = rows.map((d) => {
    const l = locateProblem(c.check_id, d);
    return `<tr><td class="mono">${esc(l.resource)}</td><td class="mono muted">${esc(l.field)}</td>
      <td class="mono">${l.value != null ? esc(l.value) : '<span class="muted" title="Withheld  a field value can be PHI"></span>'}</td>
      <td>${esc(l.problem)}</td></tr>`;
  }).join("");
  const more = c.violations > rows.length ? ` (first ${rows.length} of ${c.violations})` : ` (${rows.length})`;
  return `<details class="affected"><summary>&#9656; Show affected records${more}</summary>
    <table class="rec-table"><thead><tr><th>Record</th><th>Field</th><th>Value</th><th>What is wrong</th></tr></thead>
    <tbody>${body}</tbody></table></details>`;
}

let SOURCE = "fhir";
let LAST = null; // { passport, datasetId, sourceModel, audit }

// ---- theme (light is the MedAnon/NTT brand default) ------------------------
function applyTheme(t) {
  document.documentElement.classList.toggle("dark", t === "dark");
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.innerHTML = t === "dark" ? "&#9728;" : "&#9789;"; // sun when dark, moon when light
}
(function initTheme() {
  const saved = localStorage.getItem("tg-theme");
  applyTheme(saved || "light");
})();
document.getElementById("theme-toggle").onclick = () => {
  const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
  localStorage.setItem("tg-theme", next);
  applyTheme(next);
};

// ---- samples ---------------------------------------------------------------
const SAMPLES = {
  fhir: {
    good: JSON.stringify([
      { resourceType: "Patient", id: "p1", gender: "female", birthDate: "1980-05-01", meta: { lastUpdated: "2024-01-01T00:00:00Z" } },
      { resourceType: "Observation", id: "o1", status: "final",
        code: { coding: [{ system: "http://loinc.org", code: "2160-0" }] },
        subject: { reference: "Patient/p1" }, effectiveDateTime: "2020-01-01",
        valueQuantity: { value: 0.9, unit: "mg/dL", code: "mg/dL" } },
    ], null, 2),
    bad: JSON.stringify([
      { resourceType: "Patient", id: "p2", gender: "not-a-real-gender", birthDate: 12345 },
      { resourceType: "Observation", id: "o2", status: "final",
        code: { coding: [{ system: "http://loinc.org", code: "2160-0" }] },
        subject: { reference: "Patient/missing" } },
    ], null, 2),
  },
  omop: {
    good: JSON.stringify({
      tables: {
        person: [{ person_id: 1, gender_concept_id: 8532, year_of_birth: 1980 }],
        measurement: [{ measurement_id: 1, person_id: 1, measurement_concept_id: 3004249,
                        measurement_date: "2020-01-01", value_as_number: 100.0 }],
      },
    }, null, 2),
    bad: JSON.stringify({
      tables: {
        person: [{ person_id: 1, gender_concept_id: 8507, year_of_birth: 1990 }],
        measurement: [{ measurement_id: 1, person_id: 1, measurement_concept_id: 1,
                        measurement_date: "1980-01-01", value_as_number: 5.0 }],
      },
    }, null, 2),
  },
  tabular: {
    good: JSON.stringify({
      tables: {
        person: [{ pid: 1, sex: 8532, yob: 1980 }],
        measurement: [{ mid: 1, pid: 1, concept: 3004249, mdate: "2020-01-01", val: 100.0 }],
      },
      mapping: {
        person: { pid: "person_id", sex: "gender_concept_id", yob: "year_of_birth" },
        measurement: { mid: "measurement_id", pid: "person_id", concept: "measurement_concept_id",
                       mdate: "measurement_date", val: "value_as_number" },
      },
    }, null, 2),
    bad: JSON.stringify({
      tables: { measurement: [{ mid: 1, pid: 99, concept: 1, mdate: "1980-01-01", val: 5.0 }] },
      mapping: { measurement: { mid: "measurement_id", pid: "person_id", concept: "measurement_concept_id",
                                mdate: "measurement_date", val: "value_as_number" } },
    }, null, 2),
  },
};
const INPUT_LABELS = {
  fhir: "FHIR resource, list, or Bundle (JSON)",
  omop: "OMOP tables  { tables: { person: [...], measurement: [...] } }",
  tabular: "Source tables + column mapping  { tables: {...}, mapping: {...} }",
};

// ---- source-type tabs ------------------------------------------------------
$("src-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-src]");
  if (!b) return;
  SOURCE = b.dataset.src;
  [...$("src-seg").children].forEach((c) => c.classList.toggle("on", c === b));
  $("input-label").textContent = INPUT_LABELS[SOURCE];
});
$("sample-good").onclick = () => { $("payload").value = SAMPLES[SOURCE].good; };
$("sample-bad").onclick = () => { $("payload").value = SAMPLES[SOURCE].bad; };

// ---- service health + use cases -------------------------------------------
async function checkService() {
  const pill = $("status"), txt = $("status-txt");
  try {
    const r = await fetch("/health");
    pill.className = "status-pill " + (r.ok ? "online" : "offline");
    txt.textContent = r.ok ? "service online" : "service degraded";
  } catch { pill.className = "status-pill offline"; txt.textContent = "service offline"; }
}
async function loadUseCases() {
  try {
    const r = await fetch("/v1/use-cases");
    if (!r.ok) return;
    const body = await r.json();
    const raw = body.use_cases || body.cases || [];
    // The service returns either a list of ids/objects or an object keyed by id.
    const list = Array.isArray(raw)
      ? raw.map((u) => (typeof u === "string" ? { id: u } : u))
      : Object.entries(raw).map(([id, v]) => ({ id, description: (v && v.description) || "" }));
    const sel = $("usecase");
    for (const u of list) {
      const id = u.id;
      if (!id) continue;
      const desc = u.description || "";
      const o = document.createElement("option");
      o.value = id;
      o.textContent = id.replace(/_/g, " ") + (desc ? `  ${desc.slice(0, 60)}` : "");
      sel.appendChild(o);
    }
  } catch { /* non-fatal */ }
}
checkService();
loadUseCases();

// ---- QC pipeline stages (each maps to real passport data) ------------------
const STAGES = [
  { id: "intake",       name: "Intake & parse",                        kind: "intake",
    desc: "resources/rows normalized onto the assessment model" },
  { id: "conformance",  name: "Structural & terminology conformance",  kind: "cat", cat: "conformance" },
  { id: "completeness", name: "Completeness",                          kind: "cat", cat: "completeness" },
  { id: "plausibility", name: "Value plausibility & clinical eval",    kind: "cat", cat: "plausibility" },
  { id: "audit",        name: "Provenance & auditability",             kind: "audit" },
  { id: "decision",     name: "Scoring & deterministic decision",      kind: "decision" },
  { id: "passport",     name: "Quality Passport & EHDS label",         kind: "label" },
];

function stageResult(st, p) {
  // Returns { status:'done'|'warn'|'fail', meta:html, tag:'' }
  if (st.kind === "intake") {
    const n = p.resource_count ?? "";
    return { status: "done", meta: `${n} record(s) ingested as <b>${esc(LAST.sourceModel)}</b>` };
  }
  if (st.kind === "cat") {
    const checks = (p.checks || []).filter((c) => c.category === st.cat);
    const assessed = checks.filter((c) => c.result !== "NA");
    const failedDet = assessed.filter((c) => c.result === "FAIL" && !c.advisory);
    const failedAdv = assessed.filter((c) => c.result === "FAIL" && c.advisory);
    const score = p.category_scores ? p.category_scores[st.cat] : null;
    if (!assessed.length) return { status: "done", meta: "not assessed in this profile" };
    const passed = assessed.filter((c) => c.result === "PASS").length;
    const status = failedDet.length ? "fail" : failedAdv.length ? "warn" : "done";
    const advTag = failedAdv.length ? ` <span class="tag">${failedAdv.length} advisory</span>` : "";
    const sc = score == null ? "" : `${pct(score)}% · `;
    return { status, meta: `${sc}${passed}/${assessed.length} checks passing${failedDet.length ? ` · ${failedDet.length} failed` : ""}${advTag}` };
  }
  if (st.kind === "audit") {
    const a = p.auditability || {};
    const prov = a.provenance_present;
    const stage = p.lifecycle_stage || (p.evaluation && p.evaluation.lifecycle_stage) || "operation";
    return { status: prov ? "done" : "warn",
             meta: `provenance ${prov ? "present" : "missing"} · lifecycle <b>${esc(stage)}</b>` };
  }
  if (st.kind === "decision") {
    const d = p.decision;
    const status = d === "PASS" ? "done" : d === "BLOCK" ? "fail" : "warn";
    const basis = (p.evaluation && p.evaluation.decision_basis) || "deterministic checks";
    return { status, meta: `${pct(p.overall_score)}% overall · decision from <b>${esc(basis)}</b>` };
  }
  if (st.kind === "label") {
    const l = p.label || {};
    const q = l.quality && l.quality.decision ? l.quality.decision : "";
    const t = l.utility && l.utility.tier ? l.utility.tier : "";
    const m = l.maturity && l.maturity.level ? `L${l.maturity.level}` : "";
    return { status: "done", meta: `EHDS label issued · quality <b>${esc(q)}</b> · utility <b>${esc(t)}</b> · maturity <b>${esc(m)}</b>` };
  }
  return { status: "done", meta: "" };
}

function renderPipelineShell() {
  const rows = STAGES.map((s, i) => `
    <div class="stage pending" id="stg-${s.id}" style="animation-delay:${i * 40}ms">
      <div class="ico" id="ico-${s.id}">${i + 1}</div>
      <div class="body">
        <div class="name">${esc(s.name)}</div>
        <div class="meta" id="meta-${s.id}">${esc(s.desc || "queued")}</div>
      </div>
    </div>`).join("");
  return `<section class="card fade-in">
    <div class="hd"><h2>QC pipeline</h2><p class="note">Each stage maps to the checks that actually ran.</p></div>
    <div class="bd"><div class="pipeline"><div class="track"><i id="track-fill" style="height:0%"></i></div>${rows}</div></div>
  </section>
  <div id="report-mount"></div>`;
}

const ICONS = { done: "✓", warn: "!", fail: "✕" };
function setStage(id, cls, metaHtml, icon) {
  const stage = $(`stg-${id}`), ico = $(`ico-${id}`), meta = $(`meta-${id}`);
  if (!stage) return;
  stage.className = `stage ${cls} fade-in`;
  if (icon !== undefined) ico.innerHTML = icon;
  if (metaHtml !== undefined) meta.innerHTML = metaHtml;
}

async function runPipeline(assessPromise) {
  // Visual sweep: light each stage "running" until the response is ready.
  let responded = false, result = null, error = null;
  assessPromise.then((r) => { result = r; responded = true; }).catch((e) => { error = e; responded = true; });

  for (let i = 0; i < STAGES.length && !responded; i++) {
    setStage(STAGES[i].id, "running", "assessing…", '<span class="spin"></span>');
    $("track-fill").style.height = `${((i + 0.5) / STAGES.length) * 100}%`;
    await sleep(300);
  }
  // Wait for the network if the sweep finished first.
  while (!responded) await sleep(80);
  if (error) throw error;

  const p = result;
  LAST = { ...(LAST || {}), passport: p };
  // Finalize every stage with its real status, staggered for a stepped reveal.
  for (let i = 0; i < STAGES.length; i++) {
    const r = stageResult(STAGES[i], p);
    setStage(STAGES[i].id, r.status, r.meta, ICONS[r.status] || "✓");
    $("track-fill").style.height = `${((i + 1) / STAGES.length) * 100}%`;
    await sleep(150);
  }
  return p;
}

// ---- request building ------------------------------------------------------
function buildRequest(parsed) {
  const datasetId = $("dataset").value.trim() || "provider-dataset";
  const providerId = $("provider").value.trim() || null;
  const useCase = $("usecase").value || null;
  const common = {
    dataset_id: datasetId,
    provider_id: providerId,
    use_case: useCase,
    lifecycle_stage: $("lifecycle").value,
    org_role: $("orgrole").value,
  };
  const src = $("source").value.trim();
  if (src) common.provenance = { source_system: src };

  if (SOURCE === "fhir") {
    const resources = Array.isArray(parsed) ? parsed
      : parsed.resourceType === "Bundle" ? (parsed.entry || []).map((e) => e.resource).filter(Boolean)
      : [parsed];
    return { url: "/v1/trust/assess/batch", sourceModel: "fhir",
             body: { ...common, resources, source_types: ["fhir"] }, datasetId };
  }
  // OMOP + tabular both normalize onto OMOP server-side.
  const body = { ...common, source_types: ["omop"] };
  if (parsed.tables) body.tables = parsed.tables;
  if (parsed.mapping) body.mapping = parsed.mapping;
  if (parsed.resources) body.resources = parsed.resources;
  if (!parsed.tables && !parsed.resources) body.tables = parsed; // bare table dict
  return { url: "/v1/trust/assess/omop", sourceModel: "omop", body, datasetId };
}

// ---- run -------------------------------------------------------------------
$("run").onclick = run;
async function run() {
  $("err").textContent = "";
  let parsed;
  try { parsed = JSON.parse($("payload").value); }
  catch (e) { $("err").textContent = "Invalid JSON: " + e.message; return; }

  let req;
  try { req = buildRequest(parsed); }
  catch (e) { $("err").textContent = String(e.message || e); return; }

  LAST = { datasetId: req.datasetId, sourceModel: req.sourceModel };
  const btn = $("run");
  btn.disabled = true; btn.innerHTML = '<span class="spin-sm"></span> Running QC…';
  $("stage-col").innerHTML = renderPipelineShell();

  const assess = (async () => {
    const r = await fetch(req.url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req.body),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    return body;
  })();

  try {
    const passport = await runPipeline(assess);
    LAST.audit = await fetchAudit(req.datasetId);
    LAST.findings = await fetchFindings(req.datasetId);
    renderReport(passport);
  } catch (e) {
    $("report-mount").innerHTML =
      `<section class="card"><div class="bd err">Assessment failed: ${esc(e.message || e)}</div></section>`;
  } finally {
    btn.disabled = false; btn.textContent = "Run QC";
  }
}

async function fetchAudit(datasetId) {
  try {
    const r = await fetch(`/v1/datasets/${encodeURIComponent(datasetId)}/audit`);
    if (!r.ok) return [];
    const b = await r.json();
    return b.audit || [];
  } catch { return []; }
}
async function fetchFindings(datasetId) {
  try {
    const r = await fetch(`/v1/datasets/${encodeURIComponent(datasetId)}/findings`);
    if (!r.ok) return [];
    const b = await r.json();
    return b.findings || [];
  } catch { return []; }
}

// ===========================================================================
//  REPORT RENDERING
// ===========================================================================
const BLURB = {
  PASS: "Data meets the quality bar for its declared purpose.",
  CONDITIONAL_PASS: "Usable with caveats  review the advisory findings before release.",
  BLOCK: "Deterministic quality failures must be remediated before this data is fit for use.",
};
const GLYPH = { PASS: "&#10003;", CONDITIONAL_PASS: "&#33;", BLOCK: "&#10007;" };

function renderReport(p) {
  const mount = $("report-mount");
  mount.innerHTML = `
    <div class="tabbar" id="tabbar">
      <button class="on" data-tab="qc">Quality Passport report</button>
      <button data-tab="audit">Audit report</button>
    </div>
    <div id="tab-qc" class="tabpane">${qcReport(p)}</div>
    <div id="tab-audit" class="tabpane" style="display:none">${auditReport(p)}</div>`;
  mount.querySelector("#tabbar").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]"); if (!b) return;
    [...mount.querySelectorAll("#tabbar button")].forEach((x) => x.classList.toggle("on", x === b));
    $("tab-qc").style.display = b.dataset.tab === "qc" ? "" : "none";
    $("tab-audit").style.display = b.dataset.tab === "audit" ? "" : "none";
  });
  const copyBtn = mount.querySelector("#copy-json");
  if (copyBtn) copyBtn.onclick = () => navigator.clipboard.writeText(JSON.stringify(p, null, 2));
  const printBtn = mount.querySelector("#print-doc");
  if (printBtn) printBtn.onclick = () => window.print();
  wireTriage($("tab-qc"));
  mount.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---- QC report -------------------------------------------------------------
function qcReport(p) {
  const overall = pct(p.overall_score);
  const failed = (p.checks || []).filter((c) => c.result === "FAIL" && !c.advisory);
  const advisory = (p.checks || []).filter((c) => c.advisory);
  return [
    banner(p, overall),
    p.label ? ehdsLabel(p.label) : "",
    kpiStrip(p, failed.length),
    pillars(p),
    scorecard(p.scorecard),
    p.blockers && p.blockers.length ? blockers(p.blockers) : "",
    failedChecks(failed),
    advisory.length ? advisoryChecks(advisory, p) : "",
    findingsCard(p),
    profileCard(p.profile),
    fitnessCard(p),
    p.report ? collapsible("Full passport report (markdown)", `<pre>${esc(p.report)}</pre>`) : "",
  ].filter(Boolean).join("");
}

function banner(p, overall) {
  const d = p.decision;
  const degraded = p.degraded ? ` <span class="tag">advisory · gate degraded</span>` : "";
  return `<div class="banner b-${esc(d)}">
    <div class="glyph">${GLYPH[d] || "?"}</div>
    <div>
      <div class="title">${esc(d.replace(/_/g, " "))}${degraded}</div>
      <div class="blurb">${esc(BLURB[d] || "")}</div>
      ${p.fitness && p.fitness.statement ? `<div class="blurb" style="color:var(--fg);margin-top:6px">${esc(p.fitness.statement)}</div>` : ""}
    </div>
    <div class="score">
      <div class="pct ${rateText(overall)}">${overall}%</div>
      <div class="lbl">checks passing${p.overall_grade ? ` · grade ${esc(p.overall_grade)}` : ""}</div>
    </div>
  </div>`;
}

function ehdsLabel(l) {
  const q = l.quality || {}, u = l.utility || {}, m = l.maturity || {};
  const fairObj = l.fair || {};
  const fairKeys = Array.isArray(fairObj) ? fairObj : Object.keys(fairObj).filter((k) => fairObj[k]);
  const fair = fairKeys.length
    ? `<div class="fair">${fairKeys.map((k) => `<span class="chip">FAIR: ${esc(k)}</span>`).join("")}</div>`
    : `<div class="fair"><span class="chip" style="color:var(--muted-2)">FAIR: not yet certified</span></div>`;
  const maturitySub = Array.isArray(m.basis) ? m.basis.join(", ") : (m.name || "data-lifecycle maturity");
  return `<section class="card"><div class="hd"><h2>EHDS dataset label <span class="framework">${esc(l.scheme || "")}</span></h2></div>
    <div class="bd">
      <div class="ehds">
        <div class="lab"><div class="cap">Quality</div><div class="val ${rateText(pct(q.score))}">${esc(q.decision || q.grade || "")}</div>
          <div class="sub">${q.score != null ? pct(q.score) + "% checks passing" : "purpose-bound verdict"}${q.grade ? " · grade " + esc(q.grade) : ""}</div></div>
        <div class="lab"><div class="cap">Utility</div><div class="val">${esc(u.tier || "")}</div>
          <div class="sub">${u.score != null ? pct(u.score) + "% fit for declared use" : "fitness for declared use"}</div></div>
        <div class="lab"><div class="cap">Maturity</div><div class="val">Level ${esc(m.level ?? "")}<span class="framework"> / 5</span></div>
          <div class="sub">${esc(maturitySub)}</div></div>
      </div>
      ${fair}
    </div></section>`;
}

function kpiStrip(p, failedN) {
  const run = (p.checks || []).filter((c) => c.result !== "NA").length;
  const items = [
    ["Records", p.resource_count ?? ""],
    ["Checks run", run],
    ["Deterministic failures", failedN],
    ["Processing", p.privacy_processing_allowed ? "allowed" : "blocked"],
  ];
  return `<div class="kpis">${items.map(([k, v]) =>
    `<div class="kpi"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("")}</div>`;
}

const PILLAR_META = [
  ["conformance", "Conformance", "Format validity, FHIR/OMOP structure, coding, reference integrity."],
  ["completeness", "Completeness", "Required elements present; values or a declared reason for absence."],
  ["plausibility", "Plausibility", "Clinical ranges, uniqueness, temporal order, cross-field coherence."],
];
function pillars(p) {
  const cards = PILLAR_META.map(([key, label, desc]) => {
    const score = p.category_scores ? p.category_scores[key] : null;
    const checks = (p.checks || []).filter((c) => c.category === key && c.result !== "NA");
    const passed = checks.filter((c) => c.result === "PASS").length;
    const failed = checks.filter((c) => c.result === "FAIL").length;
    const has = score != null;
    const v = has ? pct(score) : 0;
    return `<div class="pillar">
      <div class="top"><span class="nm">${label}</span><span class="pc ${has ? rateText(v) : ""}">${has ? v + "%" : ""}</span></div>
      <div class="bar"><span style="width:${v}%;background:${rateColor(v)}"></span></div>
      <div class="counts"><span class="ok">${passed} passed</span>${failed ? `<span class="bad">${failed} failed</span>` : ""}<span style="margin-left:auto;color:var(--muted)">${checks.length} assessed</span></div>
      <div class="desc">${desc}</div>
    </div>`;
  }).join("");
  return `<div><div class="sect-title" style="margin-bottom:10px">Data quality pillars (Kahn 2016)</div><div class="pillars">${cards}</div></div>`;
}

function scorecard(sc) {
  const entries = Object.entries(sc || {});
  if (!entries.length) return "";
  const items = entries.map(([dim, s]) => {
    const g = s.grade || "?";
    const txt = s.score == null ? "not assessed" : `${pct(s.score)}% · ${s.checks_passed}/${s.checks_assessed}`;
    return `<div class="it"><span class="gradedot g-${esc(g)}">${esc(g)}</span>
      <div><div style="font-weight:600;text-transform:capitalize">${esc(dim)}</div>
      <div class="framework" style="font-size:11px">${esc(txt)}</div></div></div>`;
  }).join("");
  return `<section class="card"><div class="hd"><h2>Dimension scorecard  DAMA / ISO 25012</h2></div>
    <div class="bd"><div class="scoregrid">${items}</div></div></section>`;
}

function blockers(bs) {
  return `<section class="card" style="border-color:var(--block-bd)"><div class="hd"><h2 style="color:var(--block)">Blockers</h2></div>
    <div class="bd stack">${bs.map((b) => `<div class="check crit">${esc(b)}</div>`).join("")}</div></section>`;
}

function checkRow(c, cls) {
  const frac = c.applicable > 0 ? `${c.violations}/${c.applicable} (${pct(c.violation_fraction * 100)}%)` : "";
  const tax = [c.category, c.subcategory, c.context].filter(Boolean).join(" · ");
  return `<div class="check ${cls}">
    <div class="top"><span class="id">${esc(c.check_id)}${c.critical ? '<span class="badge critical">critical</span>' : ""}${c.advisory ? '<span class="badge advisory">advisory</span>' : ""}</span>
      <span class="frac">${esc(frac)}</span></div>
    <div class="tax">${esc(tax)}</div>
    <div class="rec"><b>What is wrong:</b> ${esc(whatItChecks(c))}</div>
    ${c.recommendation ? `<div class="rec"><b>How to fix:</b> ${esc(c.recommendation)}</div>` : ""}
    ${affectedRecords(c)}
  </div>`;
}
function failedChecks(failed) {
  if (!failed.length) return `<section class="card"><div class="hd"><h2>Deterministic checks</h2></div>
    <div class="bd note ok">All deterministic checks passed.</div></section>`;
  return `<section class="card"><div class="hd"><h2>Failed deterministic checks (${failed.length})</h2>
    <p class="note">These drive the verdict and must be remediated.</p></div>
    <div class="bd stack">${failed.map((c) => checkRow(c, c.critical ? "crit" : "fail")).join("")}</div></section>`;
}
function advisoryChecks(adv, p) {
  const seed = p.evaluation && p.evaluation.sampler_seed;
  return `<section class="card"><div class="hd"><h2>Statistical advisory (${adv.length})</h2>
    <p class="note">Reproducible (seed ${esc(seed ?? "n/a")}) but never flips the gate decision.</p></div>
    <div class="bd stack">${adv.map((c) => checkRow(c, "adv")).join("")}</div></section>`;
}

const FINDING_STATUSES = ["open", "triaged", "resolved"];
const FINDING_ROOT_CAUSES = ["unknown", "source_error", "etl_error", "genuine_biology"];

function findingRow(f, interactive) {
  const sevCls = f.severity === "critical" ? "crit" : f.severity === "minor" ? "adv" : "fail";
  const id = f.id;
  const opts = (arr, cur) => arr.map((s) => `<option value="${s}" ${cur === s ? "selected" : ""}>${esc(s.replace(/_/g, " "))}</option>`).join("");
  const controls = interactive && id ? `
    <div class="triage" data-fid="${esc(id)}">
      <select class="t-status">${opts(FINDING_STATUSES, f.status || "open")}</select>
      <select class="t-rc">${opts(FINDING_ROOT_CAUSES, f.root_cause || "unknown")}</select>
      <input class="t-note" placeholder="note (optional)" value="${esc(f.note || "")}" />
      <button class="ghost sm triage-save">Save</button>
      <span class="t-msg framework"></span>
    </div>` : "";
  return `<div class="check ${sevCls}">
    <div class="top"><span class="id">${esc(f.check_id || id)}<span class="badge ${f.severity === "critical" ? "critical" : "advisory"}">${esc(f.severity || "major")}</span></span>
      <span class="frac" data-role="status">${esc(f.status || "open")}</span></div>
    <div class="tax">root cause: ${esc(f.root_cause || "unknown")}${f.note && !interactive ? " · " + esc(f.note) : ""}</div>
    ${controls}
  </div>`;
}

function wireTriage(root) {
  if (!root) return;
  root.querySelectorAll(".triage-save").forEach((btn) => {
    btn.onclick = async () => {
      const box = btn.closest(".triage");
      const fid = box.dataset.fid;
      const msg = box.querySelector(".t-msg");
      const body = {
        status: box.querySelector(".t-status").value,
        root_cause: box.querySelector(".t-rc").value,
        note: box.querySelector(".t-note").value,
      };
      btn.disabled = true; msg.className = "t-msg framework"; msg.textContent = "saving…";
      try {
        const r = await fetch(`/v1/findings/${encodeURIComponent(fid)}/transition`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
        const updated = await r.json();
        msg.className = "t-msg ok"; msg.textContent = `saved · ${updated.status}`;
        const frac = box.closest(".check").querySelector('[data-role="status"]');
        if (frac) frac.textContent = updated.status;
      } catch (e) {
        msg.className = "t-msg bad"; msg.textContent = "failed: " + (e.message || e);
      } finally { btn.disabled = false; }
    };
  });
}

function findingsCard(p) {
  // Prefer server-derived findings (with triage); fall back to deterministic failures.
  const server = (LAST && LAST.findings) || [];
  let rows;
  if (server.length) {
    rows = server.map((f) => findingRow(f, true)).join("");
  } else {
    const fails = (p.checks || []).filter((c) => c.result === "FAIL" && !c.advisory);
    if (!fails.length) return `<section class="card"><div class="hd"><h2>Remediation findings</h2></div>
      <div class="bd note ok">No open findings  nothing to remediate.</div></section>`;
    rows = fails.map((c) => {
      const sev = c.critical ? "critical" : c.violation_fraction >= 0.5 ? "major" : "minor";
      return `<div class="check ${c.critical ? "crit" : "fail"}">
        <div class="top"><span class="id">${esc(c.check_id)}<span class="badge ${c.critical ? "critical" : "advisory"}">${sev}</span></span>
          <span class="frac">open</span></div>
        <div class="tax">${esc([c.category, c.subcategory].filter(Boolean).join(" · "))}</div>
        ${c.recommendation ? `<div class="rec">${esc(c.recommendation)}</div>` : ""}</div>`;
    }).join("");
  }
  return `<section class="card"><div class="hd"><h2>Remediation findings (PDSA)</h2>
    <p class="note">Derived from deterministic failures; track them to source-error / ETL-error / genuine-biology.</p></div>
    <div class="bd stack">${rows}</div></section>`;
}

function boxPlot(s) {
  const { min, max, mean, stddev, count, unit, code } = s;
  const q1 = Math.max(min, mean - 0.674 * stddev), q3 = Math.min(max, mean + 0.674 * stddev);
  const range = max - min, toX = (v) => (range > 0 ? ((v - min) / range) * 96 + 2 : 50);
  const fmt = (v) => Math.abs(v) >= 1000 ? Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })
                                          : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const xMin = toX(min), xMax = toX(max), xQ1 = toX(q1), xQ3 = toX(q3), xMean = toX(mean), CY = 14, BH = 9;
  return `<div class="dist">
    <div class="hd"><span class="nm">${esc(code)}${unit ? ` <span class="framework">${esc(unit)}</span>` : ""}</span><span class="n">n=${count}</span></div>
    <svg viewBox="0 0 100 28" preserveAspectRatio="none">
      <line x1="${xMin}" y1="${CY}" x2="${xMax}" y2="${CY}" stroke="var(--muted-2)" stroke-width="0.7"/>
      <rect x="${xQ1}" y="${CY - BH / 2}" width="${Math.max(xQ3 - xQ1, 0.6)}" height="${BH}" fill="rgba(79,140,255,.18)" stroke="var(--accent)" stroke-width="0.7" rx="0.6"/>
      <line x1="${xMean}" y1="${CY - BH / 2 - 2}" x2="${xMean}" y2="${CY + BH / 2 + 2}" stroke="var(--accent)" stroke-width="1.8"/>
      <line x1="${xMin}" y1="${CY - 3}" x2="${xMin}" y2="${CY + 3}" stroke="var(--muted-2)" stroke-width="0.8"/>
      <line x1="${xMax}" y1="${CY - 3}" x2="${xMax}" y2="${CY + 3}" stroke="var(--muted-2)" stroke-width="0.8"/>
    </svg>
    <div class="axis"><span>min ${fmt(min)}</span><span style="color:var(--fg);font-weight:600">mean ${fmt(mean)}</span><span>±${fmt(stddev)} sd</span><span>max ${fmt(max)}</span></div>
  </div>`;
}

function profileCard(prof) {
  if (!prof) return "";
  const blocks = [];
  if (prof.resource_counts && Object.keys(prof.resource_counts).length) {
    const total = Object.values(prof.resource_counts).reduce((a, b) => a + b, 0);
    const bars = Object.entries(prof.resource_counts).sort((a, b) => b[1] - a[1]).map(([t, n]) =>
      `<div><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px"><span style="font-weight:600">${esc(t)}</span><span class="framework">${n}</span></div>
       <div class="bar" style="margin:0"><span style="width:${total ? (n / total) * 100 : 0}%;background:var(--accent)"></span></div></div>`).join("");
    blocks.push(`<div><div class="sect-title">Record types</div><div class="stack" style="margin-top:8px">${bars}</div></div>`);
  }
  if (prof.code_system_distribution && Object.keys(prof.code_system_distribution).length) {
    const chips = Object.entries(prof.code_system_distribution).map(([u, n]) =>
      `<span class="chip">${esc(u.replace("http://", ""))} (${n})</span>`).join("");
    blocks.push(`<div><div class="sect-title">Code systems</div><div class="chips" style="margin-top:8px">${chips}</div></div>`);
  }
  if (prof.observation_value_stats && prof.observation_value_stats.length) {
    const plots = prof.observation_value_stats.map(boxPlot).join("");
    blocks.push(`<div><div class="sect-title">Clinical value distributions <span class="framework">(box = IQR, line = mean)</span></div>
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:8px">${plots}</div></div>`);
  }
  if (!blocks.length) return "";
  return `<section class="card"><div class="hd"><h2>Data profile <span class="framework">(descriptive  not scored)</span></h2></div>
    <div class="bd stack" style="gap:16px">${blocks.join("")}</div></section>`;
}

function fitnessCard(p) {
  const yes = (p.approved_for || []).map((u) => `<div class="kv"><span class="k ok">approved</span><span class="v">${esc(u)}</span></div>`).join("");
  const no = (p.not_approved_for || []).map((u) => `<div class="kv"><span class="k warn">not approved</span><span class="v">${esc(u)}</span></div>`).join("");
  if (!yes && !no) return "";
  return `<section class="card"><div class="hd"><h2>Fitness for declared use</h2></div>
    <div class="bd stack">${yes}${no}</div></section>`;
}

function collapsible(title, inner) {
  return `<details><summary>&#9656; ${esc(title)}</summary>${inner}</details>`;
}

// ---- Audit report ----------------------------------------------------------
function kvRows(obj) {
  return Object.entries(obj || {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k.replace(/_/g, " "))}</span><span class="v">${
      typeof v === "boolean" ? (v ? "yes" : "no") : Array.isArray(v) ? v.length : esc(v)}</span></div>`).join("");
}

function auditReport(p) {
  const ev = p.evaluation || {};
  const trail = (LAST && LAST.audit) || [];
  const latest = trail[0] || {};
  const identity = {
    dataset_id: LAST.datasetId,
    provider_id: $("provider").value.trim() || latest.provider_id || "",
    source_model: LAST.sourceModel,
    assessment_id: latest.assessment_id || "(not persisted)",
    generated_at: p.generated_at || latest.generated_at || "",
    recorded_at: latest.recorded_at || "",
    records: p.resource_count ?? "",
  };
  const determinism = {
    decision: p.decision,
    decision_basis: ev.decision_basis || "deterministic checks only",
    sampler_seed: ev.sampler_seed,
    advisory_checks: Array.isArray(ev.advisory_check_ids) ? ev.advisory_check_ids.length : (ev.advisory_check_ids ?? 0),
    validator_used: ev.validator_used,
    terminology_used: ev.terminology_used,
    lifecycle_stage: p.lifecycle_stage || ev.lifecycle_stage,
    org_role: p.org_role || ev.org_role,
  };
  const fw = Object.entries(p.framework_versions || {}).map(([k, v]) =>
    `<span class="chip">${esc(k)}: ${esc(v)}</span>`).join("");

  const trailHtml = trail.length
    ? `<div class="trail">${trail.map((r) => `<div class="row">
        <div><b>${esc(r.decision || "")}</b> &middot; ${esc(r.source_model || "")} &middot; <span class="framework">${esc(r.assessment_id || "")}</span></div>
        <div class="when">recorded ${esc(r.recorded_at || r.generated_at || "")}${r.provider_id ? " · provider " + esc(r.provider_id) : ""}</div>
      </div>`).join("")}</div>`
    : `<div class="note">No persisted trail for this dataset. Set <code>TRUST_GATE_STORE_DB</code> (or <code>TRUST_GATE_STORE_DB_URL</code>) on the service to retain an append-only ALCOA++ history across runs. This run's verdict is shown above.</div>`;

  const detStatement = p.decision === "BLOCK"
    ? "The verdict is BLOCK, derived solely from deterministic checks; the statistical advisory did not influence it."
    : "The verdict is derived solely from deterministic checks. Statistical advisory findings are reproducible under the recorded sampler seed and never alter the gate decision.";

  return `<section class="doc">
    <div class="dochd">
      <div><div class="t">Quality Control Audit Report</div>
        <div class="note">ALCOA++ / 21 CFR Part 11-aligned · attributable, contemporaneous, traceable</div></div>
      <span class="seal">QC verified</span>
    </div>
    <div class="docbd">
      <div><div class="sect-title">Assessment identity</div><div class="stack" style="margin-top:8px">${kvRows(identity)}</div></div>

      <div><div class="sect-title">Determinism & reproducibility</div>
        <div class="stack" style="margin-top:8px">${kvRows(determinism)}</div>
        <p class="note" style="margin-top:8px">${detStatement}</p></div>

      ${fw ? `<div><div class="sect-title">Framework & engine versions</div><div class="chips" style="margin-top:8px">${fw}</div>
        ${p.framework ? `<p class="framework" style="margin-top:8px">${esc(p.framework)}</p>` : ""}</div>` : ""}

      <div><div class="sect-title">Provenance & auditability</div><div class="stack" style="margin-top:8px">${kvRows(p.auditability) || '<div class="note">none recorded</div>'}</div></div>

      <div><div class="sect-title">ALCOA++ audit trail</div><div style="margin-top:10px">${trailHtml}</div></div>

      ${collapsible("Full passport report (markdown)", `<pre>${esc(p.report || "(none)")}</pre>`)}
      ${collapsible("Raw passport JSON", `<pre>${esc(JSON.stringify(p, null, 2))}</pre>`)}

      <div class="actions">
        <button class="ghost sm" id="print-doc">Print / save as PDF</button>
        <button class="ghost sm" id="copy-json">Copy passport JSON</button>
      </div>
    </div>
  </section>`;
}

// ===========================================================================
//  VIEW SWITCHING + HISTORY & FINDINGS
// ===========================================================================
function showView(view) {
  [...$("view-nav").children].forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  const hist = view === "history";
  document.querySelector(".rail").style.display = hist ? "none" : "";
  $("stage-col").style.display = hist ? "none" : "";
  $("history-view").style.display = hist ? "" : "none";
  if (hist && !$("hist-dataset").value && LAST && LAST.datasetId) {
    $("hist-dataset").value = LAST.datasetId;
  }
}
$("view-nav").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]"); if (b) showView(b.dataset.view);
});
$("hist-load").onclick = loadHistory;
$("hist-dataset").addEventListener("keydown", (e) => { if (e.key === "Enter") loadHistory(); });

const decisionChip = (d) =>
  `<span class="chip" style="border-color:${d === "PASS" ? "var(--pass-bd)" : d === "BLOCK" ? "var(--block-bd)" : "var(--cond-bd)"};color:${d === "PASS" ? "var(--pass)" : d === "BLOCK" ? "var(--block)" : "var(--cond)"}">${esc((d || "").replace(/_/g, " "))}</span>`;

async function loadHistory() {
  const ds = $("hist-dataset").value.trim();
  $("hist-err").textContent = "";
  if (!ds) { $("hist-err").textContent = "Enter a dataset ID."; return; }
  const btn = $("hist-load");
  btn.disabled = true; btn.innerHTML = '<span class="spin-sm"></span> Loading…';
  try {
    const get = async (path) => { const r = await fetch(path); return r.ok ? r.json() : {}; };
    const [h, t, f] = await Promise.all([
      get(`/v1/datasets/${encodeURIComponent(ds)}/history`),
      get(`/v1/datasets/${encodeURIComponent(ds)}/trend`),
      get(`/v1/datasets/${encodeURIComponent(ds)}/findings`),
    ]);
    renderHistory(ds, h.history || [], t.trend || [], f.findings || []);
  } catch (e) {
    $("hist-err").textContent = "Failed: " + (e.message || e);
  } finally { btn.disabled = false; btn.textContent = "Load history"; }
}

function trendSpark(trend) {
  if (!trend.length) return "";
  const pts = trend.slice().reverse(); // oldest -> newest
  const n = pts.length, w = 100, gap = 1.5;
  const bw = (w - gap * (n - 1)) / n;
  const bars = pts.map((p, i) => {
    const v = Math.max(0, Math.min(100, Number(p.overall_score) || 0));
    const x = i * (bw + gap), hgt = (v / 100) * 38, y = 40 - hgt;
    const col = p.decision === "PASS" ? "var(--pass)" : p.decision === "BLOCK" ? "var(--block)" : "var(--cond)";
    return `<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(hgt, 1)}" rx="0.6" fill="${col}"><title>${esc(p.generated_at)} · ${esc(p.decision)} · ${Math.round(v)}%</title></rect>`;
  }).join("");
  return `<section class="card"><div class="hd"><h2>Overall score trend <span class="framework">(oldest → newest, ${n} run${n === 1 ? "" : "s"})</span></h2></div>
    <div class="bd"><svg viewBox="0 0 100 42" preserveAspectRatio="none" style="width:100%;height:90px">${bars}</svg></div></section>`;
}

function renderHistory(ds, history, trend, findings) {
  const mount = $("history-mount");
  const latest = history[0];
  const open = findings.filter((f) => (f.status || "open") !== "resolved");

  const summary = `<div class="kpis">
    <div class="kpi"><div class="v">${history.length}</div><div class="k">runs recorded</div></div>
    <div class="kpi"><div class="v">${latest ? esc((latest.decision || "").replace(/_/g, " ")) : ""}</div><div class="k">latest verdict</div></div>
    <div class="kpi"><div class="v">${latest && latest.overall_score != null ? pct(latest.overall_score) + "%" : ""}</div><div class="k">latest score</div></div>
    <div class="kpi"><div class="v">${open.length}</div><div class="k">open findings</div></div>
  </div>`;

  const runs = history.length
    ? `<section class="card"><div class="hd"><h2>Assessment runs</h2></div><div class="bd stack">
        ${history.map((r) => `<div class="kv"><span class="k">${decisionChip(r.decision)} <span class="framework">${esc(r.generated_at || r.recorded_at || "")}</span></span>
          <span class="v ${rateText(pct(r.overall_score))}">${r.overall_score != null ? pct(r.overall_score) + "%" : ""}${r.grade ? " · " + esc(r.grade) : ""}</span></div>`).join("")}
      </div></section>`
    : `<section class="card"><div class="bd note">No persisted runs for <b>${esc(ds)}</b>. Run a QC assessment for this dataset first (the service must have a store configured).</div></section>`;

  const findCard = findings.length
    ? `<section class="card"><div class="hd"><h2>Findings  triage (${findings.length})</h2>
        <p class="note">Set status + root cause (source-error / ETL-error / genuine-biology) and save. PDSA study/act loop.</p></div>
        <div class="bd stack">${findings.map((f) => findingRow(f, true)).join("")}</div></section>`
    : `<section class="card"><div class="hd"><h2>Findings</h2></div><div class="bd note ok">No open findings for this dataset.</div></section>`;

  mount.innerHTML = summary + trendSpark(trend) + runs + findCard;
  wireTriage(mount);
}
