/* Receipts UI: zero-build vanilla JS. Streams Server-Sent Events from /api/ask or /api/replay/<id>. */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } };
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");

  let state = null;
  let health = null;

  function reset(question) {
    state = { question, evidence: {}, sentences: [], report: null, steps: {}, num: {}, active: null, replay: false };
    $("answerCard").hidden = false; $("traceCard").hidden = false; $("how").hidden = true;
    $("status").innerHTML = `<span class="badge working">Researching…</span><span class="meta">${esc(question)}</span>`;
    ["stats", "answer", "conflicts", "gaps", "repairs", "sources", "steps", "meter"].forEach((k) => ($(k).innerHTML = ""));
    $("receipt").hidden = true; $("actions").hidden = true; $("sources").hidden = true;
    $("live").textContent = "live"; $("live").className = "live on";
  }

  // ------------------------------------------------------------------ trace
  function step(key, title, detail = "") {
    if (!state.steps[key]) {
      const li = document.createElement("li");
      li.className = "step";
      li.innerHTML = `<div class="t">${esc(title)}</div><div class="d"></div><div class="rows"></div>`;
      $("steps").appendChild(li);
      state.steps[key] = li;
    }
    const li = state.steps[key];
    if (detail) li.querySelector(".d").innerHTML = detail;
    return li;
  }
  const done = (key, cls = "done") => state.steps[key] && state.steps[key].classList.add(cls);
  function row(key, html) {
    const d = document.createElement("div");
    d.className = "row"; d.innerHTML = html;
    state.steps[key].querySelector(".rows").appendChild(d);
    const tc = $("traceCard"); if (tc.scrollHeight - tc.scrollTop - tc.clientHeight < 200) tc.scrollTop = tc.scrollHeight;
    return d;
  }

  function meter(m) {
    $("meter").innerHTML = [
      [m.llm_calls, "model calls"], [(m.tokens / 1000).toFixed(1) + "k", "tokens"], ["$" + (m.cost_usd || 0).toFixed(4), "Token Factory"],
      [m.tavily_calls, "Tavily searches"], [m.tavily_credits, "Tavily credits"], [(m.elapsed_s ?? "–") + "s", "elapsed"],
    ].map(([v, l]) => `<div><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
  }

  // ------------------------------------------------------------------ answer rendering
  function citeNumbers(sentences, conflicts = []) {
    const num = {}; let n = 0;
    sentences.forEach((s) => s.cites.forEach((c) => { if (!(c in num)) num[c] = ++n; }));
    conflicts.forEach((k) => k.positions.forEach((p) => p.cites.forEach((c) => { if (!(c in num)) num[c] = ++n; })));
    return num;
  }

  function renderSentences(sentences, note = "") {
    state.sentences = sentences;
    state.num = citeNumbers(sentences, state.report ? state.report.conflicts : []);
    const html = sentences.map((s, i) => {
      const label = s.label || "unchecked";
      const mark = label === "supported" ? `<span class="mark supported" title="Fact-check: supported">✓</span>`
        : label === "partial" ? `<span class="mark partial" title="Fact-check: partly supported">~</span>` : "";
      const cites = s.cites.map((c) => `<button type="button" class="cite" data-e="${esc(c)}" data-i="${i}" aria-label="Show receipt ${state.num[c]}">${state.num[c]}</button>`).join("");
      const tag = s.kind === "inference" ? `<span class="itag" title="A conclusion drawn from the cited facts, not stated outright">inference</span>` : "";
      return `<span class="sent ${label} ${s.kind}" data-i="${i}">${esc(s.text)}${tag}</span>${mark}${cites} `;
    }).join("");
    $("answer").innerHTML = (note ? `<p class="draftnote">${esc(note)}</p>` : "") + `<p>${html}</p>`;
  }

  function showReceipt(eid, i) {
    const ev = (state.report && state.report.evidence[eid]) || state.evidence[eid];
    if (!ev) return;
    document.querySelectorAll(".cite.active, .sent.active").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(`.cite[data-e="${eid}"][data-i="${i}"]`).forEach((x) => x.classList.add("active"));
    const sEl = document.querySelector(`.sent[data-i="${i}"]`); if (sEl) sEl.classList.add("active");
    const s = state.sentences[i];
    const verdict = s && s.label ? `<div class="verdict">Fact-check of this sentence: <b>${esc(s.label)}</b>${s.reason ? " — " + esc(s.reason) : ""}${s.revised_from ? `<br>Rewritten from: <s>${esc(s.revised_from)}</s>` : ""}</div>` : "";
    const how = ev.match === "exact" ? "exact text found on the page" : ev.match === "ellipsis" ? "found on the page (passage with a gap)" : `near-exact match on the page (${Math.round(ev.match_score * 100)}%)`;
    $("receipt").innerHTML = `
      <div class="rh"><span>Receipt ${state.num[eid] ?? ""} · ${esc(eid)}</span><button type="button" class="close" aria-label="Close receipt">×</button></div>
      <p class="rq"><mark>“${esc(ev.quote)}”</mark></p>
      <div class="rs">
        <span>Source</span><b>${esc(ev.title)}</b>
        <span>Site</span><span>${esc(ev.domain)} <span class="tier ${esc(ev.tier)}">${esc(ev.tier)}</span></span>
        <span>Published</span><span>${esc(ev.published || "not stated")}</span>
        <span>Verified</span><span>${esc(how)}</span>
      </div>
      <div class="ra"><a class="primary" href="${esc(safeUrl(ev.fragment_url))}" target="_blank" rel="noopener noreferrer">Open page at this quote ↗</a>
        <a href="${esc(safeUrl(ev.url))}" target="_blank" rel="noopener noreferrer">Open page</a></div>${verdict}`;
    $("receipt").hidden = false;
    $("receipt").querySelector(".close").onclick = () => { $("receipt").hidden = true; document.querySelectorAll(".active").forEach((x) => x.classList.remove("active")); };
  }

  $("answer").addEventListener("click", (e) => {
    const c = e.target.closest(".cite");
    if (c) return showReceipt(c.dataset.e, +c.dataset.i);
    const s = e.target.closest(".sent");
    if (s) { const st = state.sentences[+s.dataset.i]; if (st && st.cites.length) showReceipt(st.cites[0], +s.dataset.i); }
  });
  $("conflicts").addEventListener("click", (e) => {
    const c = e.target.closest(".cite"); if (c) showReceipt(c.dataset.e, -1);
  });

  function renderFinal(r) {
    state.report = r;
    Object.assign(state.evidence, Object.fromEntries((r.all_evidence || []).map((e) => [e.id, e])));
    const label = { answered: "Answered", partial: "Partly answered", insufficient: "Insufficient evidence" }[r.status];
    const replay = state.replay ? ` · <span title="Recorded live run, replayed">replay of a recorded live run</span>` : "";
    const mock = r.mock ? ` · <b style="color:var(--warn)">MOCK DATA, NOT A REAL RESULT</b>` : "";
    $("status").innerHTML = `<span class="badge ${r.status}">${label}</span><span class="meta">confidence <b>${esc(r.confidence)}</b>${r.confidence_reason ? " — " + esc(r.confidence_reason) : ""} · researched ${esc(r.asked_on)}${replay}${mock}</span>`;
    const st = r.stats;
    $("stats").innerHTML = [
      `<span><b>${st.sentences_supported_final}/${st.sentences_final}</b> sentences fact-checked ✓</span>`,
      `<span><b>${st.quotes_verified}</b> quotes verified on page</span>`,
      `<span><b>${st.quotes_rejected}</b> rejected</span>`,
      `<span><b>${st.evidence_cited}</b> receipts from <b>${st.domains_cited}</b> sites${st.primary_cited ? ` (${st.primary_cited} primary)` : ""}</span>`,
      st.newest_source ? `<span>newest source <b>${esc(st.newest_source)}</b></span>` : "",
    ].join("");
    if (r.message && !r.answer.length) {
      $("answer").innerHTML = `<p class="qtitle">${esc(r.question)}</p><p class="message">${esc(r.message)}</p>`;
      state.sentences = []; state.num = {};
    } else {
      renderSentences(r.answer);
      $("answer").insertAdjacentHTML("afterbegin", `<p class="qtitle">${esc(r.question)}</p>`);
    }
    $("conflicts").innerHTML = r.conflicts.length ? `<h3>Where sources disagree</h3>` + r.conflicts.map((c) => `
      <div class="conflict"><div class="pt">${esc(c.point)}</div><div class="sides">${c.positions.map((p) => `
        <div class="side">${esc(p.claim)} ${p.cites.map((x) => `<button type="button" class="cite" data-e="${esc(x)}">${state.num[x] ?? x}</button>`).join("")}
          <div class="row">${[...new Set(p.cites.map((x) => (r.evidence[x] || {}).domain))].map(esc).join(", ")}</div></div>`).join("")}</div>
        ${c.why ? `<div class="why">Why they may differ: ${esc(c.why)}</div>` : ""}</div>`).join("") : "";
    $("gaps").innerHTML = r.gaps.length ? `<h3>What the evidence doesn't establish</h3><ul class="gaps">${r.gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>` : "";
    const rewrites = (r.repairs || []).filter((x) => x.action === "rewrite");
    $("repairs").innerHTML = (r.removed.length || rewrites.length) ? `<h3>Caught by the fact-check</h3>` +
      rewrites.map((x) => `<div class="rewrite"><s>${esc(x.before)}</s><br>→ ${esc(x.after)}<div class="why">narrowed to what the sources say</div></div>`).join("") +
      r.removed.map((x) => `<div class="removed"><s>${esc(x.text)}</s><div class="why">removed: ${esc(x.reason)}</div></div>`).join("") : "";
    $("actions").hidden = false;
    const used = new Set(Object.values(r.evidence).map((e) => e.source_id));
    $("sources").innerHTML = `<h3>All ${r.sources.length} sources read</h3><ol class="srcs">${r.sources.map((s) => `
      <li class="${used.has(s.id) ? "" : "unused"}"><a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>
      · ${esc(s.domain)} <span class="tier ${esc(s.tier)}">${esc(s.tier)}</span>${s.published ? " · " + esc(s.published) : ""}${used.has(s.id) ? " · <b>cited</b>" : ""}${s.suspicious.length ? ` · <span style="color:var(--bad)">injection-like text: ${esc(s.suspicious.join(", "))}</span>` : ""}</li>`).join("")}</ol>`;
  }

  // ------------------------------------------------------------------ events
  function onEvent(ev) {
    switch (ev.type) {
      case "start": {
        if (ev.replay) state.replay = true;
        const models = Object.entries(ev.models).map(([k, v]) => `${k}: ${v.split("/").pop()}`).join(" · ");
        step("start", "Models on Nebius Token Factory", `<span class="row">${esc(models)}</span>`); done("start");
        break;
      }
      case "plan":
        step("plan", "Plan", `${esc(ev.question_type)}${ev.needs_fresh ? ` · needs recent sources (${esc(ev.time_range || "any")})` : ""}`);
        ev.subquestions.forEach((q) => row("plan", `<b>${esc(q.id)}</b> ${esc(q.text)}`));
        done("plan"); break;
      case "search": {
        const key = "search" + ev.round;
        step(key, ev.round === 1 ? "Search (Tavily)" : `Follow-up search, round ${ev.round}`);
        const st = ev.status === "ok" ? `<span class="ok">${ev.n_results} results, ${ev.new_sources.length} new</span>` : ev.status === "empty" ? `<span class="warnc">no results</span>` : `<span class="bad">failed: ${esc(ev.error || "")}</span>`;
        row(key, `<span class="k ${esc(ev.kind)}">${esc(ev.kind)}</span>${esc(ev.query)} → ${st}${ev.broadened ? " (filter removed, retried)" : ""}${ev.why ? `<br>&nbsp;&nbsp;why: ${esc(ev.why)}` : ""}`);
        done(key); break;
      }
      case "read": {
        step("read", "Read pages and verify quotes");
        ev.accepted.forEach((e) => (state.evidence[e.id] = e));
        const rj = ev.rejected.length ? `, <span class="bad">${ev.rejected.length} rejected</span>` : "";
        const sus = ev.suspicious && ev.suspicious.length ? ` <span class="bad">⚠ injection-like text (${esc(ev.suspicious.join(", "))}), treated as data</span>` : "";
        const d = row("read", `${esc(ev.source_id)} ${esc(ev.domain)}: <span class="ok">${ev.accepted.length} verified</span>${rj}${ev.error ? ` <span class="warnc">${esc(ev.error)}</span>` : ""}${sus}`);
        if (ev.rejected.length) {
          const det = document.createElement("details"); det.className = "rej";
          det.innerHTML = `<summary>quotes not found on the page</summary>` + ev.rejected.map((x) => `<div title="${esc(x.reason)}">“${esc(x.quote)}”</div>`).join("");
          d.appendChild(det);
        }
        done("read"); break;
      }
      case "coverage": {
        const key = "cov" + ev.round;
        step(key, "Coverage audit", ev.deterministic.map((c) => `<span class="cov ${esc(c.status)}">${esc(c.subq)} ${esc(c.status)} · ${c.evidence}q/${c.domains}d</span>`).join(""));
        if (ev.stop) row(key, `stop: ${esc(ev.stop_reason)}`);
        else ev.followups.forEach((f) => row(key, `→ ${esc(f.subq)}: ${esc(f.query)}`));
        done(key, ev.stop ? "done" : "warn"); break;
      }
      case "stop":
        step("stop", "Stop searching", `${esc(ev.reason)} · ${ev.evidence} verified quotes from ${ev.sources} pages`); done("stop"); break;
      case "draft":
        step("write", "Write answer", `${ev.answer.length} sentences, each citing evidence`); done("write");
        renderSentences(ev.answer, "Draft written. Fact-checking each sentence against its quotes…"); break;
      case "check":
        step("check", "Independent fact-check", ev.answer.map((s) => ({ supported: "✓", partial: "~", unsupported: "✗" }[s.label] || "?")).join(" "));
        ev.answer.forEach((s) => { if (s.label !== "supported") row("check", `<span class="${s.label === "partial" ? "warnc" : "bad"}">${esc(s.label)}</span>: ${esc(s.text.slice(0, 90))}… — ${esc(s.reason || "")}`); });
        done("check", ev.answer.every((s) => s.label === "supported") ? "done" : "warn");
        renderSentences(ev.answer, ev.answer.some((s) => s.label !== "supported") ? "Repairing sentences that failed the fact-check…" : ""); break;
      case "repair":
        step("repair", "Repair", `${ev.revisions.filter((r) => r.action === "rewrite").length} rewritten, ${ev.removed.length} removed`);
        ev.removed.forEach((r) => row("repair", `<span class="bad">removed</span> ${esc(r.text.slice(0, 90))}`)); done("repair"); break;
      case "meter": meter(ev); break;
      case "warning": step("warn" + Math.random(), "Warning", esc(ev.message)); break;
      case "final":
        if (ev.report.stats) meter(ev.report.stats);
        renderFinal(ev.report); break;
      case "error":
        $("status").innerHTML = `<span class="badge insufficient">Stopped</span><span class="meta">${esc(ev.message)}</span>`; break;
    }
  }

  async function stream(url, opts) {
    $("askBtn").disabled = true;
    try {
      const res = await fetch(url, opts);
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done: fin } = await reader.read();
        if (fin) break;
        buf += dec.decode(value, { stream: true });
        let k;
        while ((k = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (line) { try { onEvent(JSON.parse(line.slice(6))); } catch (err) { console.error(err); } }
        }
      }
    } catch (err) {
      onEvent({ type: "error", message: `Connection problem: ${err.message}` });
    } finally {
      $("askBtn").disabled = false; $("live").textContent = "done"; $("live").className = "live";
    }
  }

  function ask(q) {
    q = (q || "").trim(); if (q.length < 4) return;
    $("q").value = q; reset(q);
    history.replaceState(null, "", "?q=" + encodeURIComponent(q));
    stream("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
  }
  function replay(ex) {
    $("q").value = ex.question; reset(ex.question); state.replay = true;
    history.replaceState(null, "", "?replay=" + encodeURIComponent(ex.id));
    stream(`/api/replay/${encodeURIComponent(ex.id)}`, {});
  }

  $("askForm").addEventListener("submit", (e) => { e.preventDefault(); ask($("q").value); });
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("q").value); } });

  function toast(msg) { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 1800); }
  $("copyMd").onclick = async () => {
    const r = await fetch("/api/markdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.report) });
    await navigator.clipboard.writeText(await r.text()); toast("Copied answer with footnoted quotes");
  };
  $("dlJson").onclick = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(state.report, null, 2)], { type: "application/json" }));
    a.download = "receipts-report.json"; a.click();
  };
  $("showSources").onclick = () => { $("sources").hidden = !$("sources").hidden; };

  async function init() {
    try {
      health = await (await fetch("/api/health")).json();
      const m = $("mode");
      m.className = "mode " + health.mode;
      m.textContent = health.mode === "live" ? "live · Token Factory + Tavily" : health.mode === "mock" ? "MOCK MODE · offline test data" : "no API keys · replays only";
    } catch { $("mode").textContent = "offline"; }
    let examples = [];
    try { examples = await (await fetch("/api/examples")).json(); } catch {}
    $("examples").innerHTML = examples.map((ex, i) => `<button type="button" class="chip" data-i="${i}">${esc(ex.label || ex.question)}${ex.recorded ? `<span class="rec" title="Replays a recorded live run instantly, no credits used">recorded</span>` : ""}</button>`).join("");
    $("examples").onclick = (e) => {
      const b = e.target.closest(".chip"); if (!b) return;
      const ex = examples[+b.dataset.i];
      if (ex.recorded && (e.shiftKey === false || !health || health.mode !== "live")) replay(ex); else ask(ex.question);
    };
    const p = new URLSearchParams(location.search);
    if (p.get("replay")) { const ex = examples.find((x) => x.id === p.get("replay")); if (ex && ex.recorded) replay(ex); }
    else if (p.get("q")) ask(p.get("q"));
  }
  init();
})();
