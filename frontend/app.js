/* TruthTrace frontend — plain JavaScript, no build step. All server text is escaped before rendering. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const link = (u, text) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text || u)}</a>` : esc(text || ""));
const day = (iso) => (iso ? String(iso).slice(0, 10) : "undated");
const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
const VCLASS = { Supported: "sup", Contradicted: "con", "Misleading Context or Framing": "mis", "Meaning Altered": "alt", "Insufficient Evidence": "ins" };
const STANCE_BADGE = { supports: "b-sup", contradicts: "b-con", mentions: "b-ins", irrelevant: "" };
const charts = [];

async function api(path, opts = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 180000);
  try {
    const res = await fetch(path, { ...opts, signal: controller.signal });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      const detail = data?.detail;
      const message = Array.isArray(detail) ? detail[0]?.msg : detail;
      throw new Error(typeof message === "string" ? message : `Request failed (${res.status}).`);
    }
    return data;
  } catch (err) {
    if (err.name === "AbortError") throw new Error("This request timed out after 3 minutes. Check that the server is still running, then try again.");
    if (err instanceof TypeError) throw new Error("Could not reach TruthTrace. Check that the server is running at http://127.0.0.1:8000.");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
const postJSON = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function verdictBadge(v) { return `<span class="badge b-${VCLASS[v] || "ins"}">${esc(v)}</span>`; }
function stanceBadge(s) { return `<span class="badge ${STANCE_BADGE[s] || ""}">${esc(s)}</span>`; }
function chart(canvas, cfg) {
  if (!window.Chart || !canvas) return;
  const c = new Chart(canvas, cfg);
  charts.push(c);
}
function clearCharts() { while (charts.length) charts.pop().destroy(); }
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function setAiStatus(ai) {
  const badge = $("#ai-badge");
  if (!ai?.enabled) badge.innerHTML = `<b>Rules-based mode</b><br>No AI model configured.`;
  else if (ai.available === false) badge.innerHTML = `<b>AI temporarily unavailable</b><br><span>${esc(ai.issue || "Using rules-based fallback.")}</span>`;
  else badge.innerHTML = `AI model ready: <b>${esc(ai.model)}</b><br><span>${esc(ai.provider)}</span>`;
}

function loading(el) {
  const started = Date.now();
  el.innerHTML = `<div class="card" role="status" aria-live="polite"><div class="row"><span class="spinner"></span><b>Checking the claim…</b></div>
    <p class="muted small loading-message">Searching public sources and reviewing evidence. This often takes 1–2 minutes; slow or rate-limited providers can add time.</p></div>`;
  const t = setInterval(() => {
    if (!document.contains(el)) { clearInterval(t); return; }
    const seconds = Math.floor((Date.now() - started) / 1000);
    const message = $(".loading-message", el);
    if (!message) return;
    if (seconds >= 120) message.textContent = "This is taking longer than usual. The request is still running; TruthTrace will show an error if it times out.";
    else if (seconds >= 45) message.textContent = "Still working. Live source providers can be slow or rate limited; your request is still running.";
  }, 5000);
  return () => clearInterval(t);
}
function showError(el, e) { el.innerHTML = `<div class="card err" role="alert">⚠ ${esc(e.message || e)}</div>`; }

function dataSources(list) {
  if (!list?.length) return "";
  return `<details><summary>Data sources used (${list.filter((s) => s.status === "ok").length}/${list.length} available)</summary>
    <table><tr><th>Source</th><th>Status</th><th>Items</th><th>Note</th></tr>
    ${list.map((s) => `<tr><td>${esc(s.source)}</td><td>${s.status === "ok" ? '<span class="badge b-sup">ok</span>' : '<span class="badge b-con">unavailable</span>'}</td>
      <td>${esc(s.count)}</td><td class="small muted">${esc(s.reason || "")}</td></tr>`).join("")}</table></details>`;
}

function evCard(it, st) {
  st = st || it.stance || {};
  return `<div class="ev st-${esc(st.stance || "")}">
    <div class="top">${st.stance ? stanceBadge(st.stance) : ""}<span class="badge">${esc(it.source_type)}</span><span>${esc(it.source)}</span>
      <span>· ${day(it.published)}</span><span>· ${esc(it.region)}${it.lang && it.lang !== "en" ? " · " + esc(it.lang) : ""}</span><span class="mono">${esc(it.id)}</span></div>
    <div class="t">${link(it.url, it.title)}</div>
    ${st.quote ? `<blockquote>“${esc(st.quote)}”</blockquote>` : it.snippet ? `<div class="small muted">${esc(it.snippet.slice(0, 260))}</div>` : ""}
    ${st.reason ? `<div class="small muted">Why: ${esc(st.reason)} <span class="badge">${esc(st.method || "")}</span>${st.quote_verified === false ? ' <span class="badge b-con">quote not verified</span>' : ""}</div>` : ""}
  </div>`;
}

/* ------------------------------------------------------------------ router */
const pages = {};
function route() {
  clearCharts();
  const [, name = "verify", arg] = location.hash.split("/");
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  const activeLink = $$("#nav a").find((a) => a.dataset.page === name);
  const section = activeLink?.closest(".nav-section");
  if (section) section.open = true;
  $("#sidebar").classList.remove("open");
  $("#menu-btn").setAttribute("aria-expanded", "false");
  const el = $("#page");
  (pages[name] || pages.verify)(el, arg);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
$("#menu-btn").onclick = () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#menu-btn").setAttribute("aria-expanded", String(open));
};

/* ------------------------------------------------------------------ verify */
const VERIFY_EXAMPLES = [
  { label: "Great Wall visible from space", text: "The Great Wall of China is visible from space with the naked eye." },
  { label: "Chandrayaan-3 landing", text: "Chandrayaan-3 landed near the south pole of the Moon in August 2023." },
  { label: "5G spreads corona (Hindi)", text: "5G नेटवर्क से कोरोना वायरस फैलता है।" },
  { label: "₹2000 note GPS chip", text: "The new 2000 rupee note has a GPS nano chip embedded in it." },
  { label: "Altered quote", text: "Minister confirms the bridge will collapse.", original: "The minister said the bridge may collapse if repairs are delayed." },
];
pages.verify = (el) => {
  el.innerHTML = `<header class="page-heading">
    <div class="eyebrow">EVIDENCE-BASED NEWS CHECK</div>
    <h1>Verify a claim</h1>
    <p class="lead">Get a clear conclusion, the sources behind it, and what remains uncertain.</p>
  </header>
  <form class="card verify-form" id="vform">
    <div class="field claim-field">
      <label for="vtext">What would you like to check?</label>
      <span class="field-note">Paste a claim, headline, or article excerpt. English, Hindi, or mixed text is fine.</span>
      <textarea class="claim-input" id="vtext" name="text" placeholder="For example: The Great Wall of China is visible from space with the naked eye."></textarea>
    </div>
    <details class="advanced-inputs">
      <summary>Add a link, media, or original quote <span class="hint">(optional)</span></summary>
      <div class="grid g2 advanced-grid">
        <div class="field"><label for="vurl">Article link</label><input type="url" id="vurl" name="url" placeholder="https://"></div>
        <div class="field"><label for="vfile">Image, video, or audio</label><input type="file" id="vfile" name="file" accept="image/*,video/*,audio/*"></div>
        <div class="field"><label for="vhead">Headline to compare</label><input type="text" id="vhead" name="headline" placeholder="Paste the article headline"></div>
        <div class="field"><label for="vorig">Original version</label><textarea id="vorig" name="original_text" placeholder="Paste the original quote or wording"></textarea></div>
      </div>
    </details>
    <div class="verify-actions">
      <button class="btn" id="vbtn">Check claim</button>
      <span class="muted small">Live sources may take a minute to review.</span>
    </div>
    <div class="examples-block"><div class="field-note">Try a sample</div>
      <div class="chips">${VERIFY_EXAMPLES.map((x, i) => `<button type="button" class="chip" data-i="${i}">${esc(x.label)}</button>`).join("")}</div>
    </div>
  </form><div id="vout"></div>`;
  $$(".chip", el).forEach((c) => (c.onclick = () => {
    const x = VERIFY_EXAMPLES[c.dataset.i];
    $("#vtext").value = x.text; $("#vorig").value = x.original || "";
    if (x.original) $("details", el).open = true;
  }));
  $("#vform").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if (!$("#vfile").files.length) fd.delete("file");
    const out = $("#vout");
    $("#vbtn").disabled = true;
    const stop = loading(out);
    try {
      const r = await api("/api/verify", { method: "POST", body: fd });
      stop();
      history.replaceState(null, "", `#/analysis/${r.analysis_id}`);
      renderVerifyResult(out, r);
    } catch (err) { stop(); showError(out, err); }
    $("#vbtn").disabled = false;
  };
};

function renderVerifyResult(out, r) {
  setAiStatus(r.ai);
  const v = r.verdict, cls = VCLASS[v.verdict] || "ins";
  const aiLabel = !r.ai?.enabled ? "Rules-based analysis" : r.ai.available === false ? "Rules-based fallback (AI unavailable)" : `AI-assisted (${esc(r.ai.model)})`;
  const mem = (r.claim_memory || []).filter((m) => m.id !== r.analysis_id);
  const byId = Object.fromEntries((r.evidence || []).map((e) => [e.id, e]));
  out.innerHTML = `
  ${mem.length ? `<div class="notice">🧠 <b>Claim memory:</b> this claim was checked before —
     ${mem.map((m) => `<a href="#/analysis/${esc(m.id)}">${day(m.created)}: ${esc(m.verdict)}${m.same_media ? " (same media file)" : ""}</a>`).join(", ")}.
     ${r.update_tracking?.changed ? `<b>The verdict has changed since the last check</b> (was ${esc(r.update_tracking.previous_verdict)}).` : ""}</div>` : ""}
  <div class="verdict v-${cls}">
    <div class="label">Overall conclusion</div>
    <div class="value">${esc(v.verdict)}</div>
    <div class="row"><span class="badge">Confidence: ${esc(v.confidence)}</span>
      <span class="badge" title="${esc(v.confidence_definition)}">what confidence means ⓘ</span>
      <span class="badge">${aiLabel}</span>
      <span class="badge">Checked ${esc(day(r.checked_at))}</span></div>
    ${r.ai?.enabled && r.ai.available === false ? `<div class="notice warn" role="status">The AI provider became unavailable during this check. Remaining steps used rule-based fallbacks${r.ai.retry_after_seconds ? `; retry in about ${Math.ceil(r.ai.retry_after_seconds / 60)} minute(s)` : ""}. The conclusion is still based on retrieved evidence.</div>` : ""}
    ${r.review?.recommended ? `<div class="notice warn" style="margin-bottom:0">👤 ${esc(r.review.note)} Reason: ${r.review.reasons.map(esc).join(", ")}.</div>` : ""}
    <p style="margin:12px 0 0">${esc(r.summary)}</p>
    ${r.summary_local?.text ? `<p class="muted" lang="hi" style="margin:8px 0 0">${esc(r.summary_local.text)} <span class="small">(${esc(r.summary_local.method)})</span></p>` : ""}
  </div>
  <div class="grid g2">
    <div class="card"><h3>How the final result is built</h3>
      <table>${r.result_parts.map((p) => `<tr><th style="width:42%">${esc(p.part)}</th><td>${esc(p.value)}</td></tr>`).join("")}</table></div>
    <div class="card"><h3>Why this verdict</h3><ol class="small" style="padding-left:18px;margin:0">${v.rule_trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
      <p class="small muted">Rules: meaning altered → contradicted (evidence weight ≥ 2× support) → misleading (supported facts, false impression) → supported → otherwise insufficient evidence. No single 0–100 score is used.</p></div>
  </div>
  <div class="card"><h3>Claim analysed</h3>
    <p style="margin:0 0 6px"><b>${esc(r.claim.main_claim)}</b></p>
    ${r.input.english && r.input.language !== "en" ? `<p class="small muted">Input language: ${esc(r.input.language)} · English: ${esc(r.input.english)}</p>` : ""}
    <div class="row small"><span class="badge">topic: ${esc(r.claim.topic)}</span><span class="badge ${r.claim.risk === "high" ? "b-con" : ""}">risk: ${esc(r.claim.risk)}</span>
      <span class="badge">extraction: ${esc(r.claim.method)}</span><span class="muted">search query: “${esc(r.claim.search_query)}”</span></div>
    <details><summary>Tests selected for this item (document §7 module rule)</summary>
      <table><tr><th>Possible problem type</th><th>Recommended tests</th></tr>${r.tests.selected.map((t) => `<tr><td>${esc(t.type)}</td><td class="small">${t.tests.map(esc).join(" · ")}</td></tr>`).join("")}</table>
      ${r.tests.skipped.map((s) => `<p class="small muted">${esc(s)}</p>`).join("")}</details>
  </div>
  <h2>12 assessment dimensions</h2>
  <p class="muted small" style="margin-top:-6px">Each dimension is calculated on its own before the overall conclusion. Click a card for findings and method.</p>
  <div class="dims" id="dims">${r.dimensions.map((d, i) => `<div class="dim s-${d.status === "assessed" ? "assessed" : d.status === "not applicable" ? "na" : "limited"}" data-i="${i}">
      <div class="num">${d.id} · ${esc(d.status)}</div><div class="nm">${esc(d.name)}</div><div class="rt">${esc(d.rating)}</div></div>`).join("")}</div>
  <div class="card" style="margin-top:16px">
    <div class="tabs" id="vtabs">${[["A", "A · Claim & Evidence"], ["B", "B · Source & Narrative"], ["C", "C · Perspectives"], ["D", "D · Public Sentiment"], ["E", "E · Influential Statements"], ["ALL", `All evidence (${r.evidence.length})`], ...(r.media ? [["M", "Media"]] : []), ["FB", "Report an error"]]
      .map(([k, l], i) => `<button class="tab ${i === 0 ? "active" : ""}" data-k="${k}">${esc(l)}</button>`).join("")}</div>
    <div id="vtab"></div>
  </div>
  ${dataSources(r.data_sources)}`;

  $$("#dims .dim").forEach((card) => (card.onclick = () => {
    $$(".dim-detail", out).forEach((x) => x.remove());
    const d = r.dimensions[card.dataset.i];
    const cols = getComputedStyle($("#dims")).gridTemplateColumns.split(" ").length;
    const idx = Number(card.dataset.i), after = Math.min(r.dimensions.length - 1, idx - (idx % cols) + cols - 1);
    const det = document.createElement("div");
    det.className = "dim-detail";
    det.innerHTML = `<b>${d.id}. ${esc(d.name)}</b> — <span class="muted">${esc(d.question)}</span>
      <p style="margin:8px 0">${esc(d.summary)}</p>${d.findings.length ? `<ul>${d.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      ${d.evidence_ids.length ? `<p class="small">Evidence: ${d.evidence_ids.map((id) => byId[id] ? link(byId[id].url, id) : esc(id)).join(", ")}</p>` : ""}
      <p class="small muted"><b>How it is measured:</b> ${esc(d.method)}</p>`;
    $$("#dims .dim")[after].after(det);
  }));
  const tabs = {
    A: () => sectionA(r, byId), B: () => sectionB(r), C: () => (r.sections.C ? renderPerspectives(r.sections.C, byId) :
      `<p class="empty">Not a political or policy item, so no viewpoint analysis was needed. Use the Perspectives dashboard to analyse any issue.</p>`),
    D: () => (r.sections.D ? renderOpinion(r.sections.D, true) : `<p class="empty">Online discussion could not be sampled.</p>`),
    E: () => renderInfluencers(r.sections.E || {}), ALL: () => r.evidence.map((it) => evCard(it, r.stances[it.id])).join("") || '<p class="empty">No evidence retrieved.</p>',
    M: () => renderMedia(r.media), FB: () => feedbackForm(r.analysis_id),
  };
  const show = (k) => {
    clearCharts();
    $$("#vtabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.k === k));
    $("#vtab").innerHTML = tabs[k]();
    afterRender($("#vtab"));
  };
  $$("#vtabs .tab").forEach((t) => (t.onclick = () => show(t.dataset.k)));
  show("A");
}

function sectionA(r, byId) {
  const A = r.sections.A;
  return `<h3>Sub-claims</h3><table><tr><th>Statement</th><th>Type</th><th>Status</th><th>Evidence</th></tr>
    ${A.subclaims.map((s) => `<tr><td>${esc(s.text)}</td><td>${esc(s.type)}</td><td>${s.status === "supported" ? '<span class="badge b-sup">supported</span>' : s.status === "contradicted" ? '<span class="badge b-con">contradicted</span>' : `<span class="badge">${esc(s.status)}</span>`}</td>
      <td class="small">${s.evidence_ids.map((id) => byId[id] ? link(byId[id].url, id) : esc(id)).join(", ")}</td></tr>`).join("")}</table>
  <div class="split" style="margin-top:14px">
    <div><h3>Evidence supporting the claim (${A.supporting.length})</h3>${A.supporting.map((it) => evCard(it)).join("") || '<p class="empty">None found.</p>'}</div>
    <div><h3>Evidence contradicting the claim (${A.contradicting.length})</h3>${A.contradicting.map((it) => evCard(it)).join("") || '<p class="empty">None found.</p>'}</div>
  </div>
  <h3>Open questions</h3>${A.open_questions.length ? `<ul>${A.open_questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>` : '<p class="empty">None recorded.</p>'}
  <p class="small muted">Independence: ${r.independence.chains.length} chain(s) among ${r.evidence.length} items — same outlet, copied text or the same wire copy count once.</p>`;
}

function sectionB(r) {
  const B = r.sections.B, f = B.first_source, vc = r.viral_card;
  return `${vc?.show ? `<h3>Viral claim status (source not established)</h3><table>
      ${[["Claim", vc.claim], ["Trend status", vc.trend_status], ["First known appearance", vc.first_known_appearance], ["Verification", vc.verification], ["Current conclusion", vc.current_conclusion]]
        .map(([k, v]) => `<tr><th style="width:28%">${k}</th><td>${esc(v)}</td></tr>`).join("")}</table>
      <p class="small muted">Wording matters: “Source not traced — authenticity unverified”, never “source not found, maybe fake”.</p>` : ""}
    <h3>First traceable source</h3>
    ${f ? `<p>${link(f.url, f.title)} — ${esc(f.source)}, ${day(f.published)}</p><p class="small muted">The earliest source found is not always the true original.</p>` : '<p class="empty">Source not traced — authenticity unverified.</p>'}
    ${B.headline_check && B.headline_check.status !== "not assessed" ? `<h3>Headline vs article</h3><p><span class="badge ${B.headline_check.status === "consistent" ? "b-sup" : "b-mis"}">${esc(B.headline_check.status)}</span></p>
      <ul>${B.headline_check.findings.map((x) => `<li><b>${esc(x.issue)}</b> — ${esc(x.detail)}${x.quote ? `<br><span class="small muted">“${esc(x.quote)}”</span>` : ""}</li>`).join("")}</ul>` : ""}
    ${B.meaning ? `<h3>Original vs circulating version</h3>${renderMeaningPair(B.meaning)}` : ""}
    <h3>How coverage presents it</h3>
    ${B.coverage.length ? `<table><tr><th>Date</th><th>Outlet</th><th>Headline</th><th>Framing</th><th>Key claim words missing</th></tr>
      ${B.coverage.map((c) => `<tr><td class="small">${day(c.published)}</td><td class="small">${esc(c.source)}<br><span class="muted">${esc(c.region)}${c.lang !== "en" ? " · " + esc(c.lang) : ""}</span></td>
        <td>${link(c.url, c.title)}</td><td class="small">${esc(c.tone.label)}${c.tone.sensational_words.length ? `<br><span class="muted">loaded: ${c.tone.sensational_words.map(esc).join(", ")}</span>` : ""}</td>
        <td class="small muted">${c.omitted.map(esc).join(", ")}</td></tr>`).join("")}</table>` : '<p class="empty">No related coverage found.</p>'}
    <p class="small muted">Negative framing is not fake news. Use Cross-Source Comparison and Claim Evolution for the full narrative view.</p>`;
}

function feedbackForm(aid) {
  setTimeout(() => {
    const f = $("#fbform");
    if (!f) return;
    f.onsubmit = async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(f));
      try { await postJSON("/api/feedback", { analysis_id: aid, ...d }); f.innerHTML = '<p class="notice">Thank you — your report is recorded and listed under Source Record &amp; Feedback for review.</p>'; }
      catch (err) { $("#fbmsg").textContent = err.message; }
    };
  });
  return `<p class="small muted">Readers and publishers can report errors or submit evidence. Reports are tracked, and how often results change is shown on the Accuracy page.</p>
    <form id="fbform"><div class="grid g2"><div class="field"><label>Type</label><select name="kind"><option>error report</option><option>new evidence</option><option>publisher appeal</option><option>other</option></select></div>
    <div class="field"><label>Evidence link (optional)</label><input type="url" name="evidence_url" placeholder="https://"></div></div>
    <div class="field"><label>Message</label><textarea name="message" required minlength="5"></textarea></div><button class="btn">Submit</button> <span id="fbmsg" class="err"></span></form>`;
}

/* ------------------------------------------------------------------ shared renderers */
function renderPerspectives(p, byId = {}) {
  const ref = (ids) => (ids || []).map((id) => byId[id] ? link(byId[id].url, id) : esc(id)).join(", ");
  const rc = p.reasoned_conclusion;
  return `${p.note ? `<div class="notice">${esc(p.note)}</div>` : ""}
  ${p.issue_dimensions?.length ? `<p>Issue dimensions: ${p.issue_dimensions.map((d) => `<span class="badge">${esc(d)}</span>`).join(" ")}</p>` : ""}
  <h3>Verifiable facts</h3>${p.verifiable_facts.length ? `<ul>${p.verifiable_facts.map((f) => `<li>${esc(f.fact)} <span class="small">[${ref(f.evidence_ids)}]</span></li>`).join("")}</ul>` : '<p class="empty">No fact could be tied to evidence.</p>'}
  <h3>Viewpoints</h3><div class="grid g2">${p.viewpoints.map((vp) => `<div class="vp"><b>${esc(vp.label)}</b><p class="small muted">${esc(vp.summary)}</p>
    ${vp.arguments.map((a) => `<div style="margin-bottom:8px">• ${esc(a.argument)} <span class="small">[${ref(a.evidence_ids)}]</span>
      ${a.factual_claims.map((c) => `<div class="small" style="margin-left:14px">${c.status === "supported" ? '<span class="badge b-sup">supported</span>' : c.status === "contradicted" ? '<span class="badge b-con">contradicted</span>' : '<span class="badge">unverified</span>'} ${esc(c.claim)} ${ref(c.evidence_ids)}</div>`).join("")}</div>`).join("")}</div>`).join("") || '<p class="empty">No viewpoints found in the retrieved coverage.</p>'}</div>
  ${p.values_vs_facts?.length ? `<h3>Values vs factual claims</h3><table><tr><th>Statement</th><th>Kind</th><th>Note</th></tr>${p.values_vs_facts.map((x) => `<tr><td>${esc(x.statement)}</td><td>${esc(x.kind)}</td><td class="small">${esc(x.note)}</td></tr>`).join("")}</table>` : ""}
  ${p.open_questions?.length ? `<h3>Questions to answer first</h3><ul>${p.open_questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}
  <h3>Reasoned conclusion</h3>${rc ? `<div class="notice"><p style="margin-top:0"><b>${esc(rc.conclusion)}</b></p>
    ${[["Evidence for", rc.evidence_for], ["Evidence against / costs", rc.evidence_against], ["Alternatives compared", rc.alternatives_compared], ["Could change this conclusion", rc.could_change_conclusion]]
      .filter(([, l]) => l?.length).map(([h, l]) => `<b class="small">${h}</b><ul class="small">${l.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`).join("")}
    ${rc.priority_scenarios?.length ? `<b class="small">How the answer changes with priorities</b><ul class="small">${rc.priority_scenarios.map((s) => `<li><b>${esc(s.priority)}:</b> ${esc(s.outcome)}</li>`).join("")}</ul>` : ""}
    <span class="badge">confidence: ${esc(rc.confidence)}</span></div>` : '<p class="empty">No conclusion generated. TruthTrace only concludes when an AI model can reason over the evidence and the evidence is sufficient.</p>'}`;
}

function renderOpinion(o, compact = false) {
  const cats = o.categories, colors = { support: css("--sup"), positive: css("--sup"), oppose: css("--con"), negative: css("--con"), neutral: css("--na"), undecided: css("--mis") };
  const s = o.sample;
  return `<div class="notice warn"><b>${esc(o.label)}.</b> Method: ${esc(o.method)}. ${esc(o.uncertainty)}</div>
  <div class="grid g4">
    <div class="card"><div class="kpi">${esc(s.collected)}</div><div class="kpi-l">posts collected</div></div>
    <div class="card"><div class="kpi">${esc(s.on_topic)}</div><div class="kpi-l">on topic</div></div>
    <div class="card"><div class="kpi">${esc(s.unique_accounts)}</div><div class="kpi-l">unique accounts (counted once)</div></div>
    <div class="card"><div class="kpi">${esc(s.exact_duplicates_removed + s.near_duplicates_removed)}</div><div class="kpi-l">duplicates / copies removed</div></div>
  </div>
  <div class="grid g2">
    <div class="card"><h3>1 · Public sentiment (per person)</h3><div class="chart-box"><canvas data-chart="opinion"></canvas></div>
      <table class="small">${cats.map((c) => `<tr><td><span class="badge" style="color:${colors[c]}">${esc(c)}</span></td><td>${o.per_person[c]} people (${o.per_person_pct[c]}%)</td><td class="muted">${o.per_post[c]} posts</td></tr>`).join("")}</table></div>
    <div class="card"><h3>Sample quality</h3><ul class="small">
      <li>Platforms: ${Object.entries(o.platforms).map(([k, v]) => `${esc(k)} ${v}`).join(", ") || "none"}</li>
      <li>Collection period: ${esc(o.collection_period.from || "—")} → ${esc(o.collection_period.to || "—")}</li>
      <li>High-volume accounts (posts collapsed to one vote): ${s.high_volume_accounts.length}</li>
      <li>Bot-like accounts flagged: ${s.bot_like_accounts.length}</li>
      <li>Coordinated copy groups (same text, ≥3 accounts): ${s.coordinated_copy_groups.length}</li></ul>
      <p class="small muted">${o.limitations.map(esc).join(" ")}</p>
      ${o.posts_per_day.length > 1 ? `<div class="chart-box" style="height:160px"><canvas data-chart="ppd"></canvas></div>` : ""}</div>
  </div>
  <div class="card"><h3>2 · Reasoning and 3 · evidence status</h3>${o.arguments.length ? o.arguments.map((a) => `<div class="vp" style="margin-bottom:8px">
      <span class="badge">${esc(a.side)}</span> <b>${esc(a.argument)}</b> <span class="muted small">(${a.post_count} posts)</span>
      <div class="small">${a.factual_claim ? `Factual claim: ${esc(a.factual_claim)} — ` : ""}<i>${esc(a.claim_status || "")}</i>${a.note ? ` · ${esc(a.note)}` : ""}</div>
      ${(a.examples || []).map((x) => `<div class="small muted">“${esc(x.text)}” ${link(x.url, "source")}</div>`).join("")}</div>`).join("") : '<p class="empty">No arguments extracted.</p>'}
    <p class="small muted">If 90% of people believe a false claim, the claim does not become true.</p></div>
  ${compact ? "" : `<div class="card"><h3>Sampled posts</h3>${o.posts.slice(0, 25).map((p) => `<div class="ev"><div class="top"><span class="badge">${esc(p.stance || p.sentiment)}</span>${esc(p.platform)} · ${day(p.published)} ${p.has_checkable_claim ? '<span class="badge b-mis">contains checkable claim — unverified</span>' : ""}</div><div class="small">${esc(p.text.slice(0, 300))} ${link(p.url, "↗")}</div></div>`).join("")}</div>`}
  <script type="application/json" data-opinion>${JSON.stringify({ cats, per_person: o.per_person, colors: cats.map((c) => colors[c]), ppd: o.posts_per_day }).replace(/</g, "\\u003c")}</script>`;
}

function renderInfluencers(inf) {
  const people = inf.people || [];
  return `<div class="notice"><b>Key rule:</b> ${esc(inf.rule || "Influential statements are kept separate from public opinion.")}</div>
  ${people.length ? people.map((p) => `<div class="card"><div class="row"><h3 style="margin:0">${esc(p.name)}</h3><span class="muted">${esc(p.role)}</span> ${p.reference ? link(p.reference, "reference") : ""}</div>
    <table class="small" style="margin:8px 0"><tr><th>Official authority on this issue</th><th>Decision-making power</th><th>Public reach</th><th>Actual policy impact</th></tr>
      <tr><td>${esc(p.influence.official_authority)}</td><td>${esc(p.influence.decision_power)}</td><td>${esc(p.influence.public_reach)}</td><td>${esc(p.influence.policy_impact)}</td></tr></table>
    <p class="small">${esc(p.primary_source)}</p>
    ${p.statements.map((s) => `<div class="ev"><div class="top">${esc(s.source)} · ${day(s.date)} <span class="badge">${esc(s.method)}</span></div>
      <blockquote>“${esc(s.quote)}”</blockquote>${s.context ? `<div class="small muted">Context: ${esc(s.context)}</div>` : ""}
      ${(s.factual_claims || []).map((c) => `<div class="small">${c.status === "supported" ? '<span class="badge b-sup">supported</span>' : c.status === "contradicted" ? '<span class="badge b-con">contradicted</span>' : '<span class="badge">unverified</span>'} ${esc(c.claim)}</div>`).join("")}
      <div class="small">${link(s.url, "Open source")}</div></div>`).join("")}
    <p class="small muted">Factual correctness is judged separately from influence. A quote from a third-party website alone is not enough.</p></div>`).join("")
    : '<p class="empty">No attributed statements by named leaders, officials or experts were found in the retrieved text.</p>'}`;
}

function renderMedia(m) {
  if (!m) return "";
  const cls = m.label.startsWith("Synthetic") ? "b-alt" : m.label.startsWith("Possible") ? "b-mis" : "b-ins";
  return `<div class="row"><span class="badge ${cls}" style="font-size:14px">${esc(m.label)}</span><span class="muted small">${esc(m.kind)} · ${esc(m.filename)} · ${esc(m.size_kb)} KB</span></div>
  <ul>${m.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><div class="notice warn small">${esc(m.warning || "")}</div>
  ${Object.values(m.checks).map((c) => `<div class="vp" style="margin-bottom:8px"><b>${esc(c.title)}</b>
    <div class="small">${typeof c.result === "string" ? esc(c.result) : `<pre class="mono">${esc(JSON.stringify(c.result, null, 2))}</pre>`}</div>
    ${c.generator_markers?.length ? `<div class="small"><b>Generator markers:</b> ${c.generator_markers.map(esc).join(", ")}</div>` : ""}
    ${c.editing_software?.length ? `<div class="small"><b>Editing software traces:</b> ${c.editing_software.map(esc).join(", ")}</div>` : ""}
    ${c.context ? `<div class="small ${c.context.severity === "high" ? "err" : ""}">${esc(c.context.finding)}</div>` : ""}
    ${c.ela ? `<div class="thumbs"><img src="${esc(c.ela.image)}" alt="Error level analysis"></div>` : ""}
    ${c.keyframes ? `<div class="thumbs">${c.keyframes.map((k) => `<figure style="margin:0"><img src="${esc(k.image)}" alt="keyframe"><figcaption class="small muted">${esc(k.time)} s</figcaption></figure>`).join("")}</div>` : ""}
    ${c.links ? `<div class="small">${c.links.map((l) => link(l.url, l.name)).join(" · ")}</div>` : ""}</div>`).join("")}
  <p class="small muted">SHA-256: <span class="mono">${esc(m.sha256)}</span>${m.average_hash ? ` · perceptual hash <span class="mono">${esc(m.average_hash)}</span>` : ""}</p>`;
}

function renderMeaningPair(m) {
  const vcls = m.verdict === "Meaning Altered" ? "b-alt" : m.verdict.startsWith("Possible") ? "b-mis" : "b-sup";
  const diff = m.diff.map((d) => d.op === "equal" ? esc(d.a) : `${d.a ? `<del>${esc(d.a)}</del>` : ""} ${d.b ? `<ins>${esc(d.b)}</ins>` : ""}`).join(" ");
  return `<div class="row"><span class="badge ${vcls}" style="font-size:14px">${esc(m.verdict)}</span><span class="badge">${esc(m.label)}</span><span class="muted small">word overlap ${pct(m.similarity)}</span></div>
  <div class="grid g2" style="margin-top:10px">
    <div class="vp"><b class="small">Original (${esc(m.original.lang)})</b><p style="margin:4px 0">${esc(m.original.text)}</p>${m.original.english !== m.original.text ? `<p class="small muted">English: ${esc(m.original.english)} — ${esc(m.original.method)}</p>` : ""}</div>
    <div class="vp"><b class="small">Later version (${esc(m.variant.lang)})</b><p style="margin:4px 0">${esc(m.variant.text)}</p>${m.variant.english !== m.variant.text ? `<p class="small muted">English: ${esc(m.variant.english)} — ${esc(m.variant.method)}</p>` : ""}</div>
  </div>
  <p class="diff small" style="margin-top:10px"><b>Word-level change (English):</b> ${diff}</p>
  ${m.changes.length ? `<table><tr><th>Change</th><th>Severity</th><th>Original phrase</th><th>Later phrase</th><th>Explanation</th></tr>${m.changes.map((c) => `<tr><td>${esc(c.type)}${c.source === "ai" ? ' <span class="badge">AI</span>' : ""}</td>
    <td>${c.severity === "high" ? '<span class="badge b-con">high</span>' : c.severity === "medium" ? '<span class="badge b-mis">medium</span>' : '<span class="badge">low</span>'}</td>
    <td>${esc(c.original_phrase)}</td><td>${esc(c.variant_phrase)}</td><td class="small">${esc(c.explanation)}</td></tr>`).join("")}</table>` : '<p class="muted">No change in numbers, negation, certainty, names or figurative wording was detected.</p>'}
  ${m.ai?.summary ? `<p class="small"><b>AI summary:</b> ${esc(m.ai.summary)}</p>` : ""}`;
}

function afterRender(root) {
  const blob = $("script[data-opinion]", root);
  if (blob) {
    const d = JSON.parse(blob.textContent);
    chart($('canvas[data-chart="opinion"]', root), { type: "doughnut", data: { labels: d.cats, datasets: [{ data: d.cats.map((c) => d.per_person[c]), backgroundColor: d.colors }] },
      options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { color: css("--ink") } } } } });
    const ppd = $('canvas[data-chart="ppd"]', root);
    if (ppd) chart(ppd, { type: "bar", data: { labels: d.ppd.map((x) => x.date), datasets: [{ label: "posts per day", data: d.ppd.map((x) => x.count), backgroundColor: css("--brand") }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: css("--muted") } }, y: { ticks: { color: css("--muted") } } } } });
  }
}

/* ------------------------------------------------------------------ simple topic pages */
function topicPage(el, { title, lead, placeholder, examples, endpoint, extra = "", render }) {
  el.innerHTML = `<h1>${esc(title)}</h1><p class="lead">${lead}</p>
  <form class="card" id="tform"><div class="field"><label for="tq">Topic, event or claim</label><input type="text" id="tq" name="query" required minlength="3" placeholder="${esc(placeholder)}"></div>${extra}
  <div class="row"><button class="btn" id="tbtn">Analyse</button><span class="muted small">Examples:</span></div>
  <div class="chips">${examples.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div></form><div id="tout"></div>`;
  $$(".chip", el).forEach((c) => (c.onclick = () => ($("#tq").value = c.textContent)));
  $("#tform").onsubmit = async (e) => {
    e.preventDefault();
    const body = Object.fromEntries([...new FormData(e.target)].filter(([, v]) => String(v).trim()));
    const out = $("#tout");
    $("#tbtn").disabled = true;
    const stop = loading(out);
    try {
      const r = await postJSON(endpoint, body);
      stop();
      history.replaceState(null, "", `#/analysis/${r.analysis_id}`);
      out.innerHTML = render(r) + dataSources(r.data_sources);
      afterRender(out);
      if (r.timeline) drawTimeline(out, r);
    } catch (err) { stop(); showError(out, err); }
    $("#tbtn").disabled = false;
  };
}

pages.compare = (el) => topicPage(el, {
  title: "Cross-Source Comparison",
  lead: "How do Indian, international and Hindi-language sources report the same event? Each headline is classified the way the project document describes reports A–D, and full articles are checked for headline–body mismatch.",
  placeholder: "e.g. pilot saved lives Air India emergency landing", examples: ["Chandrayaan-3 landing", "US H-1B visa fee", "Operation Sindoor", "Great Wall visible from space"],
  endpoint: "/api/compare", render: renderCompare,
});

function renderCompare(r) {
  const cats = ["A", "B", "C", "D"], catCls = { A: "b-sup", B: "b-mis", C: "b-alt", D: "b-con" };
  return `<div class="card"><h3>Report types</h3><div class="grid g4">${cats.map((c) => `<div><span class="badge ${catCls[c]}">${c}</span> <span class="small">${esc(r.report_types[c])}</span></div>`).join("")}</div></div>
  <div class="card"><h3>By region and language</h3><table><tr><th>Group</th><th>Items</th>${cats.map((c) => `<th>${c}</th>`).join("")}<th>Framing</th></tr>
    ${Object.entries(r.by_region).map(([k, g]) => `<tr><td><b>${esc(k)}</b></td><td>${g.count}</td>${cats.map((c) => `<td>${g.categories[c] || 0}</td>`).join("")}
      <td class="small">${Object.entries(g.tones).map(([t, n]) => `${esc(t)} ${n}`).join(" · ")}</td></tr>`).join("") || '<tr><td colspan="7" class="empty">No relevant coverage.</td></tr>'}</table>
    <p class="small">Facts most outlets agree on (terms in ≥30% of headlines): ${r.consensus_terms.map((t) => `<span class="badge">${esc(t)}</span>`).join(" ") || "—"}</p>
    ${r.hindi_query ? `<p class="small muted">Hindi search: ${esc(r.hindi_query)}</p>` : ""}<p class="small muted">${r.notes.map(esc).join(" ")}</p></div>
  <div class="card"><h3>Coverage (${r.items.length}, oldest first; ${r.excluded_irrelevant} unrelated results excluded)</h3>
  <table><tr><th>Date</th><th>Outlet</th><th>Headline</th><th>Type</th><th>Why</th></tr>
  ${r.items.map((it) => `<tr><td class="small">${day(it.published)}</td><td class="small">${esc(it.source)}<br><span class="muted">${esc(it.lang === "hi" ? "Hindi" : it.region)}</span></td>
    <td>${link(it.url, it.title)}${it.title_en ? `<div class="small muted">EN: ${esc(it.title_en)}</div>` : ""}
      ${it.headline_body ? `<div class="small">Headline vs body: <b>${esc(it.headline_body.status)}</b>${it.headline_body.findings.slice(0, 2).map((f) => ` · ${esc(f.issue)}`).join("")}</div>` : ""}</td>
    <td><span class="badge ${catCls[it.report.category] || ""}">${esc(it.report.category)}</span></td>
    <td class="small">${esc(it.report.why)}${it.report.added ? `<br>Added: ${esc(it.report.added)}` : ""}${it.report.omitted_ai ? `<br>Omitted: ${esc(it.report.omitted_ai)}` : ""}<br><span class="muted">${esc(it.report.tone.label)}</span></td></tr>`).join("")}</table></div>`;
}

pages.evolution = (el) => topicPage(el, {
  title: "Claim Evolution & Narrative Tracking",
  lead: "Connect the original statement to every later version in time order. TruthTrace marks the first version that added a new claim and the first one disputed by evidence. Optionally paste the original statement to compare every version against it.",
  placeholder: "e.g. Chandrayaan-3 south pole landing", examples: ["5G coronavirus", "2000 rupee note GPS chip", "Great Wall visible from space", "UNESCO best national anthem"],
  extra: `<div class="field"><label for="torig">Original statement <span class="hint">(optional)</span></label><input type="text" id="torig" name="original"></div>`,
  endpoint: "/api/evolution", render: renderEvolution,
});

function renderEvolution(r) {
  const f = r.first_known_appearance;
  return `<div class="grid g3">
    <div class="card"><div class="kpi-l">First known appearance</div>${f ? `<p style="margin:6px 0">${link(f.url, f.title)}</p><p class="small muted">${esc(f.source)} · ${day(f.published)}</p>` : '<p class="empty">Not traced</p>'}<p class="small muted">${esc(r.first_appearance_note)}</p></div>
    <div class="card"><div class="kpi">${r.versions.length}</div><div class="kpi-l">versions found</div><p class="small muted">spanning ${r.span_days} days</p></div>
    <div class="card"><div class="kpi-l">Compared against</div><p class="small" style="margin:6px 0">“${esc(r.reference)}”</p><span class="badge">${esc(r.reference_kind)}</span></div></div>
  <div class="card"><h3>Versions over time</h3><div class="chart-box"><canvas data-chart="timeline"></canvas></div></div>
  <div class="card"><h3>Version chain</h3>${r.versions.map((v) => `<div class="stage ${v.evolution.disputed || v.evolution.adds_new_claim ? "broken" : ""}">
    <div class="row small"><b>${day(v.published)}</b><span>${esc(v.source)}</span><span class="badge">${esc(v.evolution.label)}</span>
      ${v.id === r.first_added_claim_id ? '<span class="badge b-alt">first version adding a new claim</span>' : v.evolution.adds_new_claim ? '<span class="badge b-alt">adds claim</span>' : ""}
      ${v.id === r.first_disputed_id ? '<span class="badge b-con">first disputed version</span>' : v.evolution.disputed ? '<span class="badge b-con">disputed / fact-check</span>' : ""}</div>
    <div>${link(v.url, v.title)}</div>${v.title_en ? `<div class="small muted">EN: ${esc(v.title_en)}</div>` : ""}
    ${v.evolution.changes.length ? `<div class="small muted">${v.evolution.changes.map((c) => esc(c.explanation)).join(" ")}</div>` : ""}</div>`).join("") || '<p class="empty">No versions found.</p>'}</div>`;
}

function drawTimeline(root, r) {
  const c = $('canvas[data-chart="timeline"]', root);
  if (c) chart(c, { type: "bar", data: { labels: r.timeline.map((x) => x.date), datasets: [{ label: "versions published", data: r.timeline.map((x) => x.count), backgroundColor: css("--brand") }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: css("--muted") } }, y: { ticks: { color: css("--muted"), precision: 0 } } } } });
}

pages.perspectives = (el) => topicPage(el, {
  title: "Perspectives (POV)",
  lead: "For political and policy issues: verifiable facts, the real viewpoints found in coverage, an evidence check of each side's factual claims, values separated from facts, and — when evidence allows — a reasoned conclusion showing how it was reached.",
  placeholder: "e.g. Great Nicobar Island development project", examples: ["Great Nicobar Island development project", "One Nation One Election", "US H-1B visa fee increase", "Agnipath scheme"],
  endpoint: "/api/perspectives",
  render: (r) => `<div class="card">${renderPerspectives(r, Object.fromEntries(r.evidence.map((e) => [e.id, e])))}</div>
    <details class="card"><summary>Evidence used (${r.evidence.length})</summary>${r.evidence.map((e) => evCard(e, {})).join("")}</details>`,
});

pages.opinion = (el) => topicPage(el, {
  title: "Public Opinion Dashboard",
  lead: "What people online are saying — shown as three separate things: sentiment, reasoning, and whether the reasons contain verified claims. Each account counts once; duplicates and copied posts are removed.",
  placeholder: "e.g. H-1B visa fee", examples: ["H-1B visa fee", "electric vehicles India", "work from home", "UPI transaction charges"],
  extra: `<div class="field"><label for="tpos">Position to measure support/opposition for <span class="hint">(optional; needs AI model — e.g. “The visa fee increase is good policy”)</span></label><input type="text" id="tpos" name="position"></div>`,
  endpoint: "/api/opinion",
  render: (r) => renderOpinion(r) + (r.news_volume?.length ? "" : '<p class="small muted">Global news volume (GDELT) was unavailable for this topic.</p>'),
});

pages.influencers = (el) => topicPage(el, {
  title: "Influential People Dashboard",
  lead: "What leaders, officials, institutions and experts said — exact words, role, source and the factual status of claims inside the statement. Kept as a separate scoreboard: never added to public opinion as votes.",
  placeholder: "e.g. US tariffs on India", examples: ["US tariffs on India", "Chandrayaan-3", "COVID vaccine safety", "Great Nicobar project"],
  endpoint: "/api/influencers",
  render: (r) => `<div class="card">${renderInfluencers(r)}</div>`,
});

/* ------------------------------------------------------------------ meaning */
const CHAIN_EXAMPLE = ["The leader's stomach looks swollen.", "The leader looks like a pregnant woman.", "The leader became pregnant."];
pages.meaning = (el) => {
  el.innerHTML = `<h1>Meaning &amp; Translation Analysis</h1>
  <p class="lead">Did translation or rewriting change the meaning? TruthTrace checks numbers, negation, certainty (“may” vs “did”), names and places, and whether a description or joke became a literal claim — and shows the exact phrase where it changed.</p>
  <div class="tabs"><button class="tab active" data-m="pair">Compare two versions</button><button class="tab" data-m="chain">Track a chain of versions</button></div>
  <form class="card" id="mpair"><div class="grid g2">
    <div class="field"><label>Original statement</label><textarea name="original" required>प्रधानमंत्री ने कहा कि पायलटों ने 134 लोगों की जान बचाई।</textarea></div>
    <div class="field"><label>Later version / translation</label><textarea name="variant" required>PM says pilots saved 143 lives.</textarea></div></div>
    <button class="btn">Compare</button></form>
  <form class="card" id="mchain" hidden><div class="field"><label>One version per line, oldest first</label><textarea name="versions" style="min-height:120px">${CHAIN_EXAMPLE.map(esc).join("\n")}</textarea></div>
    <p class="small muted">The default example is the document's hypothetical illustration (not a real news sequence).</p><button class="btn">Find where meaning changed</button></form>
  <div id="mout"></div>`;
  $$(".tabs .tab", el).forEach((t) => (t.onclick = () => {
    $$(".tabs .tab", el).forEach((x) => x.classList.toggle("active", x === t));
    $("#mpair").hidden = t.dataset.m !== "pair"; $("#mchain").hidden = t.dataset.m !== "chain"; $("#mout").innerHTML = "";
  }));
  const run = async (body, form) => {
    const button = $("button:not([type]), button[type=submit]", form);
    button.disabled = true;
    const out = $("#mout"); const stop = loading(out);
    try { const r = await postJSON("/api/meaning", body); out.innerHTML = `<div class="card">${r.stages ? renderChain(r) : renderMeaningPair(r)}</div>`; }
    catch (err) { showError(out, err); }
    finally { stop(); button.disabled = false; }
  };
  $("#mpair").onsubmit = (e) => { e.preventDefault(); run(Object.fromEntries(new FormData(e.target)), e.target); };
  $("#mchain").onsubmit = (e) => { e.preventDefault(); run({ versions: new FormData(e.target).get("versions").split("\n").filter((x) => x.trim()) }, e.target); };
};

function renderChain(r) {
  return `<p><b>${esc(r.summary)}</b></p>${r.stages.map((s) => `<div class="stage ${s.index === r.first_altered_stage ? "broken" : ""}">
    <div class="row small"><b>Stage ${s.index}</b><span class="badge ${s.verdict === "Meaning Altered" ? "b-alt" : ""}">${esc(s.label)}</span>${s.index ? `<span class="muted">vs previous: ${esc(s.verdict)} · vs original: ${esc(s.verdict_vs_original)}</span>` : ""}</div>
    <div>${esc(s.text)}</div>${s.english && s.english !== s.text ? `<div class="small muted">EN: ${esc(s.english)}</div>` : ""}
    ${s.diff ? `<div class="diff small">${s.diff.map((d) => d.op === "equal" ? esc(d.a) : `${d.a ? `<del>${esc(d.a)}</del>` : ""} ${d.b ? `<ins>${esc(d.b)}</ins>` : ""}`).join(" ")}</div>` : ""}
    ${s.changes.filter((c) => c.severity !== "low").map((c) => `<div class="small">→ <b>${esc(c.type)}</b>: ${esc(c.explanation)}</div>`).join("")}</div>`).join("")}`;
}

/* ------------------------------------------------------------------ media */
pages.media = (el) => {
  el.innerHTML = `<h1>Media Check</h1><p class="lead">The five checks for images, video and audio (document §8). Detector-style signals are indicators, never the final judge; the result stays “inconclusive” unless provenance or dated metadata says otherwise.</p>
  <form class="card" id="mform"><div class="field"><label>Image, video or audio file</label><input type="file" name="file" required accept="image/*,video/*,audio/*"></div>
  <label class="small" style="font-weight:400"><input type="checkbox" name="claims_recent" value="true"> The file is being shared as showing a <b>recent / new</b> event</label>
  <div style="margin-top:10px"><button class="btn">Analyse</button></div></form><div id="mdout"></div>`;
  $("#mform").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target, button = $("button:not([type]), button[type=submit]", form), out = $("#mdout");
    button.disabled = true;
    const stop = loading(out);
    try { const r = await api("/api/media", { method: "POST", body: new FormData(form) }); out.innerHTML = `<div class="card">${renderMedia(r)}</div>`; }
    catch (err) { showError(out, err); }
    finally { stop(); button.disabled = false; }
  };
};

/* ------------------------------------------------------------------ history */
const KIND_LABEL = { verify: "Verification", compare: "Cross-source", evolution: "Evolution", meaning: "Meaning", "meaning-chain": "Meaning chain", perspectives: "Perspectives", opinion: "Public opinion", influencers: "Influential people", media: "Media" };
pages.history = async (el) => {
  el.innerHTML = `<h1>History &amp; Claim Memory</h1><p class="lead">Every analysis is stored locally. When the same claim returns — in new words or with the same media file — the earlier result is shown and verdict changes are tracked.</p><div id="hout"><span class="spinner"></span></div>`;
  try {
    const rows = await api("/api/history");
    $("#hout").innerHTML = rows.length ? `<div class="card"><table><tr><th>Date</th><th>Type</th><th>Item</th><th>Result</th></tr>
      ${rows.map((r) => `<tr><td class="small">${esc(r.created.replace("T", " ").slice(0, 16))}</td><td>${esc(KIND_LABEL[r.kind] || r.kind)}</td>
        <td><a href="#/analysis/${esc(r.id)}">${esc(r.title)}</a></td><td>${r.verdict ? (VCLASS[r.verdict] ? verdictBadge(r.verdict) : `<span class="badge">${esc(r.verdict)}</span>`) : ""} ${r.confidence && r.confidence !== "Not applicable" ? `<span class="small muted">${esc(r.confidence)}</span>` : ""}</td></tr>`).join("")}</table></div>`
      : '<p class="empty">Nothing yet — run a verification first.</p>';
  } catch (err) { showError($("#hout"), err); }
};

pages.analysis = async (el, id) => {
  el.innerHTML = `<span class="spinner"></span>`;
  try {
    const row = await api(`/api/analysis/${encodeURIComponent(id)}`);
    const r = { ...row.result, analysis_id: row.id };
    el.innerHTML = `<p class="small"><a href="#/history">← History</a> · ${esc(KIND_LABEL[row.kind] || row.kind)} · ${esc(row.created)}</p><h1>${esc(row.title)}</h1><div id="aout"></div>`;
    const out = $("#aout");
    const R = { compare: renderCompare, evolution: renderEvolution, meaning: renderMeaningPair, "meaning-chain": renderChain,
      perspectives: (x) => renderPerspectives(x, Object.fromEntries(x.evidence.map((e) => [e.id, e]))), opinion: (x) => renderOpinion(x), influencers: renderInfluencers, media: renderMedia };
    if (row.kind === "verify") renderVerifyResult(out, r);
    else { out.innerHTML = `<div class="card">${R[row.kind](r)}</div>${dataSources(r.data_sources)}`; afterRender(out); if (r.timeline) drawTimeline(out, r); }
    if (row.feedback?.length) out.insertAdjacentHTML("beforeend", `<div class="card"><h3>Reader feedback on this result</h3>${row.feedback.map((f) => `<p class="small"><span class="badge">${esc(f.status)}</span> ${esc(f.kind)}: ${esc(f.message)} ${link(f.evidence_url, "evidence")}</p>`).join("")}</div>`);
  } catch (err) { showError(el, err); }
};

/* ------------------------------------------------------------------ sources & feedback */
pages.sources = async (el) => {
  el.innerHTML = `<h1>Source Record &amp; Feedback</h1><p class="lead">Source reliability over time: how each domain's evidence lined up with TruthTrace's final verdicts — a running record, not a one-time brand rating. Below, reader reports and appeals.</p><div id="sout"><span class="spinner"></span></div>`;
  try {
    const [srcs, fb] = await Promise.all([api("/api/sources"), api("/api/feedback")]);
    $("#sout").innerHTML = `<div class="card"><h3>Source history</h3>${srcs.length ? `<table><tr><th>Domain</th><th>Type</th><th>Times used</th><th>Aligned with verdict</th><th>Backed a claim later rated Contradicted</th><th>Disputed a claim rated Supported</th><th>Seen</th></tr>
      ${srcs.map((s) => `<tr><td>${esc(s.domain)}</td><td>${esc(s.source_type)}</td><td>${s.n}</td><td>${s.aligned || 0}</td><td>${s.backed_false || 0}</td><td>${s.disputed_true || 0}</td><td class="small">${day(s.first_seen)} → ${day(s.last_seen)}</td></tr>`).join("")}</table>` : '<p class="empty">No sources recorded yet.</p>'}
      <p class="small muted">These counts depend on TruthTrace's own verdicts, which can be wrong; they are a transparency record, not a judgement of the outlet.</p></div>
    <div class="card"><h3>Reader reports and appeals</h3>${fb.length ? `<table><tr><th>Date</th><th>Analysis</th><th>Type</th><th>Message</th><th>Status</th></tr>
      ${fb.map((f) => `<tr><td class="small">${day(f.created)}</td><td><a href="#/analysis/${esc(f.analysis_id)}">${esc(f.title || f.analysis_id)}</a><br>${f.verdict ? `<span class="small muted">${esc(f.verdict)}</span>` : ""}</td>
        <td>${esc(f.kind)}</td><td class="small">${esc(f.message)} ${link(f.evidence_url, "evidence")}</td>
        <td><span class="badge">${esc(f.status)}</span><div class="row" style="margin-top:4px">${["upheld", "rejected"].map((s) => `<button class="btn ghost small" data-fid="${f.id}" data-s="${s}">${s}</button>`).join("")}</div></td></tr>`).join("")}</table>` : '<p class="empty">No reports yet.</p>'}</div>`;
    $$("[data-fid]", el).forEach((b) => (b.onclick = async () => { await postJSON(`/api/feedback/${b.dataset.fid}/status`, { status: b.dataset.s }); pages.sources(el); }));
  } catch (err) { showError($("#sout"), err); }
};

/* ------------------------------------------------------------------ accuracy */
pages.accuracy = async (el) => {
  el.innerHTML = `<h1>System Accuracy</h1><p class="lead">If a system claims high accuracy, we must know which data it was tested on and what mistakes it made (document §15).</p><div id="aout"><span class="spinner"></span></div>`;
  try {
    const d = await api("/api/evaluation");
    const m = d.meaning, c = d.claims;
    const metricRows = (x) => `<tr><td>Precision</td><td>${pct(x.precision)}</td></tr><tr><td>Recall</td><td>${pct(x.recall)}</td></tr><tr><td>F1 score</td><td>${pct(x.f1)}</td></tr><tr><td>False positive rate</td><td>${pct(x.false_positive_rate)}</td></tr>`;
    $("#aout").innerHTML = `
    <div class="card"><h3>Claim verification test set ${c ? `<span class="badge">${esc(c.mode.enabled ? "AI: " + c.mode.model : "heuristic mode")}</span> <span class="small muted">run ${esc(c.run_at)}</span>` : ""}</h3>
      ${c ? `<div class="grid g4"><div><div class="kpi">${pct(c.accuracy)}</div><div class="kpi-l">accuracy (n=${c.n})</div></div><div><div class="kpi">${pct(c.abstention_rate)}</div><div class="kpi-l">abstention rate</div></div>
        <div><div class="kpi">${pct(c.accuracy_when_answered)}</div><div class="kpi-l">accuracy when answered</div></div><div><div class="kpi">${pct(c.fake_detection.false_positive_rate)}</div><div class="kpi-l">genuine news called false</div></div></div>
        <div class="grid g3" style="margin-top:12px"><div><h3>Problem detection (contradicted / altered / misleading)</h3><table>${metricRows(c.fake_detection)}</table></div>
          <div><h3>Calibration</h3><table><tr><th>Stated confidence</th><th>n</th><th>Accuracy</th></tr>${Object.entries(c.calibration).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${pct(v.accuracy)}</td></tr>`).join("")}</table></div>
          <div><h3>Language-wise</h3><table><tr><th>Language</th><th>n</th><th>Accuracy</th></tr>${Object.entries(c.by_language).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${pct(v.accuracy)}</td></tr>`).join("")}</table></div></div>
        <h3 style="margin-top:12px">Cases</h3><table><tr><th>Claim</th><th>Expected</th><th>Predicted</th><th>Chains (+/−)</th></tr>${c.cases.map((x) => `<tr><td class="small">${esc(x.text)}</td><td>${verdictBadge(x.expected)}</td><td>${verdictBadge(x.predicted)} ${x.correct ? "✓" : "✗"}</td><td class="small">${x.support_chains} / ${x.contradict_chains}</td></tr>`).join("")}</table>
        <p class="small muted">${esc(c.note)}</p>` : `<p class="empty">Not run yet.</p>`}
      <p class="small">Run it yourself (takes a few minutes, needs internet): <code class="mono">python -m eval.run_eval</code></p></div>
    <div class="card"><h3>Meaning-drift detector test set</h3>
      <div class="grid g3"><div><div class="kpi">${pct(m.accuracy)}</div><div class="kpi-l">accuracy (n=${m.cases.length})</div></div>
        <div><table>${metricRows(m.metrics)}</table></div>
        <div><table><tr><th>Language pair</th><th>n</th><th>Accuracy</th></tr>${Object.entries(m.by_language_pair).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${pct(v.accuracy)}</td></tr>`).join("")}</table></div></div>
      <details><summary>Cases</summary><table><tr><th>ID</th><th>Pair</th><th>Expected altered</th><th>Predicted</th><th>Changes detected</th></tr>${m.cases.map((x) => `<tr><td>${esc(x.id)}</td><td>${esc(x.pair)}</td><td>${x.expected_altered}</td><td>${x.predicted_altered} ${x.correct ? "✓" : "✗"}</td><td class="small">${x.changes.map(esc).join(", ")}</td></tr>`).join("")}</table></details>
      <p class="small muted">${esc(m.note)}</p></div>
    <div class="card"><h3>Reader feedback</h3><p>${d.feedback.feedback} report(s) received, ${d.feedback.feedback_upheld} upheld (result changed).</p>
      <p class="small muted">Testing warnings from the document: near-duplicate items must never appear in both practice and test sets, and new events must be included — a system can memorise claims from public benchmarks.</p></div>`;
  } catch (err) { showError($("#aout"), err); }
};

/* ------------------------------------------------------------------ method */
pages.method = (el) => {
  const dims = [["Claim accuracy", "Verified sub-claims ÷ checkable sub-claims"], ["Source reliability", "Source type, transparency signals, correction history"], ["Original-source traceability", "Earliest appearance, primary item, gaps"],
    ["Independent corroboration", "Independent chains, not article counts"], ["Contradiction and evidence quality", "Directness, source type, recency"], ["Translation and meaning accuracy", "Numbers, negation, certainty, names"],
    ["Headline and framing distortion", "Headline–body match, hidden caveats"], ["Time and context accuracy", "Event time vs publication, old-as-new"], ["Image and video authenticity", "Credentials, reverse search, forensics"],
    ["Audio and speaker authenticity", "Quote tracing, transcript match"], ["Public sentiment and spread", "Unique accounts, duplicates — not a truth score"], ["Expert, official and influential statements", "Exact quote, role, primary source"]];
  el.innerHTML = `<h1>Method &amp; Limits</h1><p class="lead">TruthTrace is not a “fake or real” detector. It shows the evidence, the source, the context, and the journey of each claim — and says “we do not know yet” when that is the honest answer.</p>
  <div class="grid g2">
    <div class="card"><h3>Four verification stages</h3><ol class="small"><li><b>Claim Verification</b> — what is claimed and does credible evidence support it?</li><li><b>Cross-Source Comparison</b> — how Indian, international and Hindi sources report it; do headlines match articles?</li>
      <li><b>Meaning &amp; Translation Analysis</b> — did translation or rewriting change the meaning?</li><li><b>Claim Evolution</b> — which later version added a claim, which is contradicted?</li></ol></div>
    <div class="card"><h3>Five verdicts</h3><table class="small">
      <tr><td>${verdictBadge("Supported")}</td><td>Available evidence supports the claim.</td></tr><tr><td>${verdictBadge("Contradicted")}</td><td>Reliable evidence disproves the claim.</td></tr>
      <tr><td>${verdictBadge("Misleading Context or Framing")}</td><td>Facts may be right, presentation creates a false impression.</td></tr>
      <tr><td>${verdictBadge("Meaning Altered")}</td><td>Translation or rewriting changed the original meaning.</td></tr><tr><td>${verdictBadge("Insufficient Evidence")}</td><td>Not enough evidence yet to conclude.</td></tr></table></div></div>
  <div class="card"><h3>The 12 assessment dimensions</h3><div class="grid g3">${dims.map(([n, d], i) => `<div class="small"><b>${i + 1}. ${esc(n)}</b><br><span class="muted">${esc(d)}</span></div>`).join("")}</div></div>
  <div class="grid g2">
    <div class="card"><h3>Key principles</h3><ul class="small"><li>Negative framing is not fake news.</li><li>Source not found is not proof of falsehood.</li><li>Popular does not mean true.</li><li>Influence is not accuracy.</li>
      <li>No single test gives a 100% guarantee; the system stops when evidence is not enough.</li><li>Reach cannot be proven from news websites alone.</li></ul></div>
    <div class="card"><h3>Anti-fabrication safeguards</h3><ul class="small"><li>All evidence comes from live HTTP responses (news indexes, web search, Wikipedia, public social APIs); every item links to its source.</li>
      <li>Any quote an AI model cites is matched against the downloaded source text; unmatched quotes are discarded and not counted.</li><li>Evidence ids an AI model cites that do not exist are removed.</li>
      <li>Unavailable data sources are listed as “unavailable”, never filled in.</li><li>Without an AI model, no reasoned conclusion or stance-of-people is invented.</li></ul></div></div>
  <div class="card"><h3>Honest limitations</h3><ul class="small"><li>Stance detection without an AI model uses keyword and polarity rules and can misread sarcasm or complex sentences; confidence is capped at Medium in that mode.</li>
    <li>Only public, key-free sources are searched (Google/Bing News RSS, DuckDuckGo, Wikipedia, Reddit, Bluesky, Hacker News, Lemmy, GDELT). Paywalled articles and closed platforms (WhatsApp, X, Facebook) are not covered.</li>
    <li>Media checks are indicators: there is no free reverse-image API and no validated deepfake detector, so results are usually “inconclusive”.</li>
    <li>Online opinion samples are biased; they are labelled “sampled online discussion”.</li><li>Quality differs by language; Hindi relies on translation.</li><li>Opponents adapt; methods need regular re-testing.</li></ul></div>`;
};

/* ------------------------------------------------------------------ boot */
(async () => {
  try {
    const s = await api("/api/status");
    setAiStatus(s.ai);
  } catch { $("#ai-badge").textContent = "Server not reachable"; }
  route();
})();
