/* Local manual-review UI. No external dependencies. */
"use strict";

const STATUS_LABEL = { passed: "通过", failed: "不通过", uncertain: "存疑", unreviewed: "未审核" };
const SCOPE_LABEL = { case_definition: "案例定义", run_answer: "运行回答" };
const DRAFT_PREFIX = "mr-draft:v1:";
window.__reviewErrors = [];
window.__reviewBootId = String(Date.now()) + "-" + Math.random().toString(36).slice(2);
window.addEventListener("error", (event) => {
  window.__reviewErrors.push(String(event.message || "script error"));
});
window.addEventListener("unhandledrejection", (event) => {
  window.__reviewErrors.push(String((event.reason && event.reason.message) || event.reason || "unhandled rejection"));
});

const state = {
  ready: false,
  meta: null,
  split: "development",
  scope: "case_definition",
  filter: "all",
  rows: [],
  // The single source of truth for list highlight / main panel selection.
  // It is deliberately independent from state.review (the last saved record).
  selectedCaseId: null,
  detail: null,
  runs: [],
  runId: null,
  runResult: null,
  judgeResult: null,
  judgeError: null,
  review: null,
  historical: [],
  selectedStatus: "",
  formNotes: "",
  formReviewer: "local-user",
  dirty: false,
  saving: false,
  conflict: null,
  holdoutExposed: false,
  holdoutCount: 0,
  draftNotice: "",
  pendingRunFromHash: null,
  loadSeq: 0,
  listSeq: 0,
  progress: null,
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function shortHash(hash, n = 12) {
  if (!hash) return "—";
  return hash.length > n ? hash.slice(0, n) + "…" : hash;
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(value) {
  return STATUS_LABEL[value] || "未审核";
}

function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.className = "toast" + (kind === "error" ? " error" : kind === "success" ? " success" : "");
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 3800);
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body) {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Requested-With"] = "manual-review";
  }
  const response = await fetch(path, opts);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (err) { data = { raw: text }; }
  if (!response.ok) {
    const message = (data && (data.message || (data.detail && data.detail.message))) || `请求失败 (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.data = data && data.detail ? data.detail : data;
    throw error;
  }
  return data;
}

function currentCaseHash() {
  return state.detail ? state.detail.caseHash : "";
}

function draftKey() {
  if (!state.detail) return "";
  const scope = state.scope;
  const run = scope === "run_answer" ? (state.runId || "none") : "case";
  return `${DRAFT_PREFIX}${state.detail.case.id}:${scope}:${run}:${currentCaseHash()}`;
}

function readDraft() {
  const key = draftKey();
  if (!key) return null;
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

function writeDraft() {
  const key = draftKey();
  if (!key || !state.detail) return;
  const payload = {
    status: state.selectedStatus,
    notes: $("#notes").value,
    reviewer: $("#reviewer").value,
    baseRevision: state.review ? Number(state.review.revision || 0) : 0,
    savedAt: new Date().toISOString(),
  };
  try { localStorage.setItem(key, JSON.stringify(payload)); } catch (err) { /* storage may be full */ }
}

function clearDraft() {
  const key = draftKey();
  if (key) {
    try { localStorage.removeItem(key); } catch (err) { /* ignore */ }
  }
}

// Clear only the draft that was actually persisted by this save request.
// Never clear whatever case happens to be selected when the response arrives.
function clearDraftKeyIfSaved(key, payload) {
  if (!key) return;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return;
    const draft = JSON.parse(raw);
    const same = (draft.status || "") === (payload.status || "")
      && (draft.notes || "") === (payload.notes || "")
      && (draft.reviewer || "") === (payload.reviewer || "");
    if (same) localStorage.removeItem(key);
  } catch (err) { /* ignore malformed draft */ }
}

function draftDiffersFromRecord(draft, record) {
  if (!draft) return false;
  const baseStatus = record ? record.status : "";
  const baseNotes = record ? (record.notes || "") : "";
  const baseReviewer = record ? (record.reviewer || "") : "";
  return Boolean(draft.status) && (
    draft.status !== baseStatus ||
    (draft.notes || "") !== baseNotes ||
    (draft.reviewer || "") !== baseReviewer
  );
}

function setSaveState(text, kind) {
  const node = $("#save-state");
  node.textContent = text;
  node.dataset.state = kind || "idle";
}

function reviewBlockedByNoRun() {
  return state.scope === "run_answer" && !state.runId;
}

function setDirty(dirty) {
  state.dirty = Boolean(dirty);
  $("#dirty-dot").hidden = !state.dirty;
  if (state.dirty) {
    $("#save-btn").disabled = reviewBlockedByNoRun() || !state.selectedStatus;
    setSaveState("未保存 · 草稿已保留", "dirty");
  }
}

function markDirty() {
  setDirty(true);
  writeDraft();
}

function guardDirty() {
  if (!state.dirty) return true;
  return window.confirm("当前修改尚未保存。草稿已保留在本地，切换后回到此条可恢复。\n确定现在切换吗？");
}

function syncHash() {
  const params = new URLSearchParams();
  if (state.split === "holdout") params.set("split", "holdout");
  if (state.selectedCaseId) params.set("case", state.selectedCaseId);
  params.set("scope", state.scope);
  if (state.scope === "run_answer" && state.runId) params.set("run", state.runId);
  const next = "#" + params.toString();
  if (window.location.hash !== next) {
    history.replaceState(null, "", next);
  }
}

function parseHash() {
  return new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
}

function updateProgress() {
  if (!state.progress) return;
  const definition = state.progress.case_definition || {};
  const run = state.progress.run_answer || {};
  const defTotal = Number(definition.total || 0);
  const defReviewed = Number(definition.reviewed || 0);
  const runTotal = Number(run.eligible || run.total || 0);
  const runReviewed = Number(run.reviewed || 0);
  $("#progress-definition-text").textContent = `${defReviewed} / ${defTotal}`;
  $("#progress-run-text").textContent = `${runReviewed} / ${runTotal}`;
  $("#progress-definition-bar").style.width = defTotal ? `${Math.max(2, (defReviewed / defTotal) * 100)}%` : "0";
  $("#progress-run-bar").style.width = runTotal ? `${Math.max(2, (runReviewed / runTotal) * 100)}%` : "0";
}

function badge(label, status) {
  return `<span class="mini-badge ${escapeHtml(status)}">${escapeHtml(label)}：${escapeHtml(statusLabel(status))}</span>`;
}

// Re-paint only the selection marker.  The highlight is always derived from
// state.selectedCaseId (the case the user actually opened), never from the
// last saved review record.
function paintSelection() {
  const selected = state.selectedCaseId;
  document.querySelectorAll("#case-list .case-item").forEach((item) => {
    const active = Boolean(selected) && item.dataset.caseId === selected;
    item.classList.toggle("active", active);
    item.setAttribute("aria-current", active ? "true" : "false");
  });
}

function renderList() {
  const list = $("#case-list");
  if (state.split === "holdout" && !state.holdoutExposed) {
    list.innerHTML = "";
    return;
  }
  if (!state.rows.length) {
    list.innerHTML = `<div class="empty-inline" style="padding:18px 8px">当前筛选没有案例。</div>`;
    $("#list-count").textContent = "0 条";
    return;
  }
  const html = state.rows.map((row) => {
    const active = row.caseId === state.selectedCaseId ? " active" : "";
    const runBadge = row.hasRuns
      ? badge("回答", row.review.run_answer.status)
      : `<span class="mini-badge unreviewed">回答：无运行</span>`;
    return `
      <button class="case-item${active}" type="button" data-case-id="${escapeHtml(row.caseId)}" aria-current="${row.caseId === state.selectedCaseId}">
        <div class="case-item-top">
          <span class="case-item-title">${escapeHtml(row.title || row.caseId)}</span>
        </div>
        <div class="case-item-id">${escapeHtml(row.caseId)} · ${escapeHtml(row.category || "")}</div>
        <div class="case-item-badges">
          ${badge("案例", row.review.case_definition.status)}
          ${runBadge}
        </div>
      </button>`;
  }).join("");
  list.innerHTML = html;
  paintSelection();
  $("#list-count").textContent = `${state.rows.length} 条${state.split === "holdout" ? " · 留出集" : ""}`;
}

function showHoldoutGate(show) {
  $("#holdout-gate").hidden = !show;
  $("#case-list").hidden = show;
}

async function fetchList() {
  const split = state.split;
  let url = `/api/cases?split=${encodeURIComponent(split)}&filter=${encodeURIComponent(state.filter)}&scope=${encodeURIComponent(state.scope)}`;
  if (split === "holdout") url += "&include_holdout=1";
  return api(url);
}

async function loadList({ autoSelect = false } = {}) {
  if (state.split === "holdout" && !state.holdoutExposed) {
    showHoldoutGate(true);
    $("#list-count").textContent = "隐藏";
    return null;
  }
  showHoldoutGate(false);
  const seq = ++state.listSeq;
  try {
    const data = await fetchList();
    if (seq !== state.listSeq) return null;
    state.rows = data.rows || [];
    state.progress = data.progress || null;
    updateProgress();
    renderList();
    if (autoSelect) {
      const visible = state.rows.some((row) => row.caseId === state.selectedCaseId);
      if (!visible) {
        if (state.rows.length) {
          await selectCase(state.rows[0].caseId, { skipGuard: true });
        } else {
          clearWorkspace("当前筛选没有匹配的案例");
        }
      }
    }
    return data;
  } catch (error) {
    if (seq !== state.listSeq) return null;
    if (error.status === 403 && state.split === "holdout") {
      state.holdoutExposed = false;
      showHoldoutGate(true);
      $("#list-count").textContent = "隐藏";
      toast("留出集仍为隐藏状态；请先点击“查看留出集”。", "error");
      return null;
    }
    toast(error.message || "案例列表加载失败", "error");
    return null;
  }
}

async function refreshListOnly() {
  if (state.split === "holdout" && !state.holdoutExposed) return;
  const seq = ++state.listSeq;
  try {
    const data = await fetchList();
    if (seq !== state.listSeq) return;
    state.rows = data.rows || [];
    state.progress = data.progress || null;
    updateProgress();
    renderList();
  } catch (error) {
    /* keep the current form; list refresh is non-critical */
  }
}

function clearWorkspace(message) {
  state.selectedCaseId = null;
  state.detail = null;
  state.runResult = null;
  state.review = null;
  state.historical = [];
  state.dirty = false;
  $("#case-view").hidden = true;
  const empty = $("#empty-state");
  empty.hidden = false;
  empty.querySelector("h2").textContent = message || "选择左侧案例开始审核";
  empty.querySelector("p").textContent = "案例定义与运行回答是两个独立审核对象，分别保存，不互相覆盖。";
  $("#empty-run-entry").hidden = state.split === "holdout" && !state.holdoutExposed;
  renderScopeCards();
  paintSelection();
  syncHash();
}

function renderCaseBase() {
  const detail = state.detail;
  const c = detail.case;
  $("#empty-state").hidden = true;
  $("#case-view").hidden = false;
  $("#case-eyebrow").textContent = `${c.split} · ${c.id}`;
  $("#case-title").textContent = c.title || c.id;
  $("#case-id").textContent = c.id;
  $("#case-category").textContent = c.category || "—";
  $("#case-split").textContent = c.split || "—";
  $("#case-hash").textContent = shortHash(detail.caseHash, 14);
  $("#case-hash").title = detail.caseHash;
  $("#case-suite").textContent = detail.suiteRevision || "—";
  $("#turn-count").textContent = `${(c.turns || []).length} 轮 · ${c.origin && c.origin.kind ? c.origin.kind : "origin 未标"}`;

  const hashPanel = $("#hash-panel");
  hashPanel.innerHTML = `
    <div><strong>caseHash</strong>（排除 review 元数据，绑定 case 定义）</div>
    <code>${escapeHtml(detail.caseHash)}</code>
    <div style="margin-top:6px"><strong>suiteRevision</strong> ${escapeHtml(detail.suiteRevision)} · <strong>suiteId</strong> ${escapeHtml(detail.suiteId)}</div>
    <div>cases.jsonl <code>${escapeHtml(state.meta?.suite?.casesSha256 || "")}</code></div>
    <div>split.json <code>${escapeHtml(state.meta?.suite?.splitSha256 || "")}</code></div>
    <div>suite.json <code>${escapeHtml(state.meta?.suite?.suiteSha256 || "")}</code></div>
    <div style="margin-top:6px">原始 dataset / split / hash 只读；审核记录写入独立 store。</div>`;

  renderTurns();
  renderEvidence();
  renderScopeCards();
}

function renderTurns() {
  const turns = (state.detail.case.turns || []);
  const container = $("#turns");
  if (!turns.length) {
    container.innerHTML = `<div class="empty-inline" style="padding:16px">该 case 没有 turns。</div>`;
    return;
  }
  container.innerHTML = turns.map((turn, idx) => {
    const role = turn.role || "unknown";
    const expected = turn.expected || {};
    const claims = expected.required_claims || [];
    const forbidden = expected.forbidden_claims || [];
    const behaviors = expected.required_behaviors || [];
    const sources = expected.expected_sources || [];
    const stateKeys = expected.state && Object.keys(expected.state).length ? expected.state : null;
    const expectedHtml = `
      <div class="expected">
        <div class="expected-grid">
          <div class="expected-block">
            <h4>required claims</h4>
            ${claims.length ? `<ul>${claims.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<div class="empty-inline">无</div>`}
          </div>
          <div class="expected-block">
            <h4>forbidden claims</h4>
            ${forbidden.length ? `<ul>${forbidden.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<div class="empty-inline">无</div>`}
          </div>
          <div class="expected-block">
            <h4>required behaviors</h4>
            ${behaviors.length ? `<ul>${behaviors.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<div class="empty-inline">无</div>`}
          </div>
          <div class="expected-block">
            <h4>expected state</h4>
            ${stateKeys ? `<pre>${escapeHtml(JSON.stringify(stateKeys, null, 2))}</pre>` : `<div class="empty-inline">空（无状态期望）</div>`}
          </div>
          <div class="expected-block" style="grid-column:1 / -1">
            <h4>expected sources（可接受候选，不是自动语义支持）</h4>
            ${sources.length ? `<div class="sources">${sources.map((item) => `<code>${escapeHtml(item)}</code>`).join(" · ")}</div>` : `<div class="empty-inline">无</div>`}
          </div>
        </div>
      </div>`;
    return `
      <div class="turn">
        <div class="turn-head">
          <span class="turn-index">第 ${idx + 1} 轮</span>
          <span class="role-pill ${escapeHtml(role)}">${escapeHtml(role)}</span>
        </div>
        <p class="turn-content">${escapeHtml(turn.content || "")}</p>
        ${role === "assistant_fixture" ? `<div class="fixture-note">assistant_fixture 只是上下文回放，不是真实模型输出。</div>` : ""}
        ${expectedHtml}
      </div>`;
  }).join("");
}

function renderEvidence() {
  const evidence = state.detail.case.evidence || [];
  const container = $("#evidence-list");
  if (!evidence.length) {
    container.innerHTML = `<div class="empty-inline">该 case 没有 evidence 记录。</div>`;
    return;
  }
  container.innerHTML = evidence.map((item) => `
    <div class="evidence">
      <div class="evidence-head">
        <span class="evidence-file">${escapeHtml(item.file || "—")}</span>
        <span class="evidence-kind">${escapeHtml(item.kind || "evidence")}</span>
      </div>
      <p class="evidence-quote">${escapeHtml(item.quote || "")}</p>
      <div class="evidence-sha">sha256 ${escapeHtml(shortHash(item.sha256 || "", 16))}${item.topic ? " · " + escapeHtml(item.topic) : ""}</div>
    </div>`).join("");
}

function renderScopeCards() {
  document.querySelectorAll(".scope-card").forEach((button) => {
    const active = button.dataset.scope === state.scope;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  placeRunPanel();
}

function describeStatus(status) {
  const label = statusLabel(status);
  if (status === "unreviewed") return `<span class="tag">${label}</span>`;
  const cls = status === "passed" ? "green" : status === "failed" ? "red" : "amber";
  return `<span class="tag ${cls}">${label}</span>`;
}

function renderRunPanel() {
  const body = $("#run-body");
  const note = $("#run-panel-note");
  const title = $("#run-panel-title");
  const panel = $("#run-panel");
  const runs = state.runs || [];
  if (title) title.textContent = state.scope === "run_answer" ? "Agent 实际回答" : "运行快照（只读）";
  if (panel) panel.classList.toggle("scope-primary", state.scope === "run_answer");
  if (!runs.length) {
    note.textContent = state.scope === "run_answer" ? "该 case 暂无 run；不会自动触发模型" : "只展示已有 development 运行";
    body.innerHTML = `
      <div class="run-toolbar">
        <label for="run-select">选择 run</label>
        <select id="run-select" disabled><option>暂无可用 run</option></select>
        <span class="hint-inline">按 run 分开保存运行回答审核</span>
      </div>
      <div class="run-no-result" data-run-empty="no-runs">
        <strong>暂无运行结果</strong>
        <div>该 case 没有可用的 development run 快照。不会为了审核自动触发模型，也不会运行 holdout；expected 不是 actual。</div>
      </div>`;
    placeRunPanel();
    return;
  }
  note.textContent = state.scope === "run_answer"
    ? "当前审核对象为运行回答；expected 不参与 actual"
    : "只读参考；案例定义判定不因运行答案改变";
  const options = runs.map((run) => {
    const flags = [];
    if (run.mode) flags.push(run.mode);
    if (run.regrade) flags.push("regrade");
    if (run.historical) flags.push("历史快照");
    const when = run.startedAt ? fmtTime(run.startedAt) : "时间未知";
    return `<option value="${escapeHtml(run.runId)}" ${run.runId === state.runId ? "selected" : ""}>${escapeHtml(run.runId)} · ${escapeHtml(flags.join(" / "))} · ${escapeHtml(when)}</option>`;
  }).join("");
  const selectedMeta = runs.find((run) => run.runId === state.runId) || null;
  const metaTags = selectedMeta ? `
    <div class="run-meta">
      ${describeStatus(selectedMeta.status || "unreviewed")}
      ${selectedMeta.mode ? `<span class="tag">${escapeHtml(selectedMeta.mode)}</span>` : ""}
      ${selectedMeta.regrade ? `<span class="tag">regrade</span>` : ""}
      ${selectedMeta.historical ? `<span class="tag amber">历史快照</span>` : `<span class="tag green">当前 hash</span>`}
      <span class="tag">cases ${escapeHtml(shortHash(selectedMeta.casesSha256 || "", 10))}</span>
    </div>` : "";
  let runHtml = "";
  if (state.runResult && state.runResult.available) {
    const result = state.runResult;
    if (result.run && result.run.historical) {
      runHtml += `<div class="historical-banner">历史快照：该 run 的 cases.jsonl hash 与当前数据集不一致。这里只展示当时运行的回答；不要把它当成当前题目的实际结果，也不要用它改 split / 冻结文件。</div>`;
    }
    if (!result.hasAnyAnswer) {
      runHtml += `<div class="run-no-result" data-run-empty="no-answer"><strong>该 run 没有回答文本</strong><div>${escapeHtml(result.message || "该 run 存在，但没有回答文本（例如状态检查运行）。")} expected 只是题目预期，不能当作 Agent 实际回答。</div></div>`;
    } else {
      const turns = (result.turns || []).filter((turn) => turn.hasAnswer);
      runHtml += turns.map((turn) => {
        const idx = Math.max(0, Number(turn.index || 1) - 1);
        const userTurn = (state.detail.case.turns || [])[idx];
        const userText = userTurn ? userTurn.content : "（该轮用户输入未找到）";
        return `
          <div class="run-turn">
            <div class="turn-head">
              <span class="turn-index">第 ${idx + 1} 轮</span>
              <span class="role-pill user">用户输入</span>
            </div>
            <p class="turn-content">${escapeHtml(userText)}</p>
            <div class="answer-block">
              <div class="answer-label">运行回答（actual，不是 expected）</div>
              <p class="answer-text">${escapeHtml(turn.answer)}</p>
            </div>
            ${turn.traceId ? `<div class="run-expected-note">trace_id ${escapeHtml(turn.traceId)} · latency ${escapeHtml(String(turn.latencyMs ?? "—"))} ms</div>` : ""}
          </div>`;
      }).join("");
      const trace = result.trace || {};
      const checks = result.checks || [];
      const checkCounts = checks.reduce((acc, item) => {
        const key = item.status || "unknown";
        acc[key] = (acc[key] || 0) + 1;
        return acc;
      }, {});
      runHtml += `
        <details class="trace-box" ${state.scope === "run_answer" ? "open" : ""}>
          <summary>trace / 自动检查摘要（只读，不是人工得分）</summary>
          <div class="trace-inner">
            <div class="trace-row"><b>trace_ids</b><span>${escapeHtml((trace.traceIds || []).join(", ") || "—")}</span></div>
            <div class="trace-row"><b>events</b><span>${escapeHtml(String(trace.eventCount ?? "—"))} 个 · ${escapeHtml((trace.eventTypes || []).slice(0, 16).join(", ") || "—")}</span></div>
            <div class="trace-row"><b>tools</b><span>${escapeHtml((trace.tools || []).join(", ") || "—")}</span></div>
            <div class="trace-row"><b>errors</b><span>${escapeHtml((trace.errors || []).join(" | ") || "无")}</span></div>
            <div class="trace-row"><b>row status</b><span>${escapeHtml(result.rowStatus || "—")}</span></div>
            <div class="trace-row"><b>checks</b><span>${escapeHtml(Object.entries(checkCounts).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—")}</span></div>
            <div class="trace-row"><b>note</b><span>运行结果只读；人工审核不写回 run，也不作为自动得分。</span></div>
          </div>
        </details>`;
    }
  } else if (state.runResult && state.runResult.available === false) {
    runHtml = `<div class="run-no-result" data-run-empty="run-unavailable"><strong>暂无运行结果</strong><div>${escapeHtml(state.runResult.message || "该 run 没有此 case 的结果。")}</div></div>`;
  } else {
    runHtml = `<div class="run-no-result" data-run-empty="loading"><strong>加载运行快照…</strong><div>不会触发模型。</div></div>`;
  }
  body.innerHTML = `
    <div class="run-toolbar">
      <label for="run-select">选择 run</label>
      <select id="run-select">${options}</select>
      <span class="hint-inline">按 run 分开保存运行回答审核</span>
    </div>
    ${metaTags}
    ${runHtml}`;
  const select = $("#run-select");
  if (select) {
    select.addEventListener("change", () => selectRun(select.value));
  }
  placeRunPanel();
}

// In run-answer scope the actual answer must be the first thing in the main
// column; case-definition keeps the existing turns -> evidence -> run order.
function placeRunPanel() {
  const main = $("#main-column");
  const panel = $("#run-panel");
  if (!main || !panel) return;
  if (state.scope === "run_answer") {
    if (main.firstElementChild !== panel) main.insertBefore(panel, main.firstElementChild);
  } else if (main.lastElementChild !== panel) {
    main.appendChild(panel);
  }
  placeJudgePanel();
}

// Judge advice always follows the actual-answer / run panel.  It is a
// separate read-only panel: it never participates in the review form or notes.
function placeJudgePanel() {
  const main = $("#main-column");
  const run = $("#run-panel");
  const judge = $("#judge-panel");
  if (!main || !run || !judge) return;
  if (run.nextElementSibling !== judge) run.insertAdjacentElement("afterend", judge);
}

function judgeVerdictLabel(value) {
  return {
    passed: "通过",
    failed: "不通过",
    uncertain: "存疑",
    not_applicable: "不适用",
  }[value] || (value || "—");
}

function judgeStatusLabel(value) {
  return { completed: "已完成", error: "错误", partial: "部分完成" }[value] || (value || "—");
}

function renderJudgePanel() {
  const body = $("#judge-body");
  const note = $("#judge-panel-note");
  if (!body) return;
  placeJudgePanel();
  if (note) {
    const run = state.runId ? ` · run ${state.runId}` : "";
    note.textContent = `dsh-judge/v1 · 只读${run} · 不写人工状态 / 备注`;
  }
  const data = state.judgeResult;
  if (state.judgeError) {
    body.innerHTML = `
      <div class="judge-empty" data-judge-empty="error">
        <strong>Judge 结果加载失败</strong>
        <div>${escapeHtml(state.judgeError)}</div>
        <div>只读结果不可用；人工审核状态与备注没有被修改。</div>
      </div>`;
    return;
  }
  if (!data) {
    body.innerHTML = `
      <div class="judge-empty" data-judge-empty="loading">
        <strong>加载 Judge 结果…</strong>
        <div>只读已完成的 DSH Judge 结果；不会触发模型，也不改人工判定。</div>
      </div>`;
    return;
  }
  if (!data.available || !data.selected) {
    const historical = data.historical || [];
    const errors = data.errors || [];
    let historyHtml = "";
    if (historical.length) {
      historyHtml = `<div class="judge-alert warn" data-judge-historical="1"><b>历史快照：caseHash 或 run 不匹配当前选择</b><div>${escapeHtml(data.message || "")}</div></div>` +
        historical.slice(0, 5).map((item) => `
          <div class="judge-role">
            <div class="judge-role-head"><strong>${escapeHtml(item.judgeRunId)}</strong>
              <span class="tag amber">历史</span>
              <span class="tag">${escapeHtml(judgeStatusLabel(item.status))} / ${escapeHtml(judgeVerdictLabel(item.verdict))}</span>
              <span class="tag">run ${escapeHtml(item.runId)}</span>
            </div>
            <div class="judge-role-reason">caseHash ${escapeHtml(shortHash(item.caseHash, 14))} ≠ 当前 ${escapeHtml(shortHash(data.currentCaseHash, 14))}</div>
          </div>`).join("");
    }
    const errorsHtml = errors.length
      ? `<div class="judge-alert error" data-judge-errors="1"><b>存在格式错误的结果，不能当通过</b><ul class="judge-errors">${errors.map((item) => `<li>${escapeHtml(item.judgeRunId)} · ${escapeHtml(item.code)} · ${escapeHtml(item.message)}</li>`).join("")}</ul></div>`
      : "";
    body.innerHTML = `
      <div class="judge-empty" data-judge-empty="no-result">
        <strong>暂无 Judge 建议</strong>
        <div>${escapeHtml(data.message || "该 case + run 暂无 Judge 结果。")}</div>
        <div>需由独立 DSH Judge 任务生成后写入 <code>eval/judge_results/&lt;judge_run_id&gt;/&lt;case_id&gt;.json</code>；本页面不会触发模型。</div>
      </div>
      ${historyHtml}
      ${errorsHtml}`;
    return;
  }
  const item = data.selected;
  const policy = item.scorePolicy || {};
  const included = Boolean(policy.included);
  const evidenceTrust = item.evidenceTrust || null;
  const coverage = item.coverage || null;
  const coverageCounts = item.aggregationCounts || null;
  const coverageMissing = item.coverageMissingIssueIds || [];
  const coverageUnbound = item.coverageUnboundIssueIds || [];
  const coverageUncertain = item.coverageUncertainIssueIds || [];
  const coverageUnresolved = item.coverageUnresolvedIssueIds || [];
  const statusBad = item.status !== "completed";
  const alerts = [];
  if (statusBad) {
    alerts.push(`<div class="judge-alert error" data-judge-status="${escapeHtml(item.status)}"><b>Judge 状态为 ${escapeHtml(judgeStatusLabel(item.status))}，不足以判定。</b> 即使 verdict 字段写着 ${escapeHtml(judgeVerdictLabel(item.verdict))}，也不能当成通过；该结果不进语义评分分母。</div>`);
  }
  if (item.error) {
    alerts.push(`<div class="judge-alert error"><b>错误</b> ${escapeHtml(item.error)}</div>`);
  }
  if (policy.reviewTrust && policy.reviewTrust !== "trusted" && !statusBad) {
    const trustLabel = {
      legacy_unverified: "旧 contract / 无完整 coverage，仅历史展示",
      coverage_missing: "缺 coverage，版本号本身不足以信任",
      coverage_incomplete: "coverage 不完整，保持存疑",
      system_errors: "system_errors 非空，机器结果不完整",
      provisional_unresolved: "存在未决仲裁，暂定",
      historical: "历史快照",
      wrong_run: "绑定其他 run",
      evaluation_only: "failure 只由 evaluation 问题支撑",
    }[policy.reviewTrust] || policy.reviewTrust;
    alerts.push(`<div class="judge-alert warn" data-judge-trust="${escapeHtml(policy.reviewTrust)}"><b>评审可信度：${escapeHtml(trustLabel)}</b> 该结果保留展示，但不把它当作最终 Agent 结论或可信评分。</div>`);
  }
  if (item.aggregationMismatch) {
    alerts.push(`<div class="judge-alert error" data-judge-aggregation-mismatch="1"><b>聚合 verdict 与顶层 verdict 不一致</b> ${escapeHtml(item.aggregationCaseVerdict || "—")} ≠ ${escapeHtml(item.verdict || "—")}；不能据此计分。</div>`);
  }
  if (evidenceTrust && evidenceTrust.checked > 0) {
    if (evidenceTrust.unverified > 0) {
      alerts.push(`<div class="judge-alert warn" data-judge-quote-trust="${escapeHtml(String(evidenceTrust.unverified))}"><b>本地未追溯到的 Judge 引用 ${escapeHtml(String(evidenceTrust.unverified))}/${escapeHtml(String(evidenceTrust.checked))} 条</b> 本地只覆盖 dataset case + 当前 run 白名单文本；未追溯 ≠ 伪造，也可能是输入证据包中合法的派生 rubric/规范文本。保持存疑，不据此判 Agent 错。</div>`);
    }
  }
  if ((item.systemErrors || []).length) {
    alerts.push(`<div class="judge-alert error" data-judge-system-errors="${escapeHtml(String(item.systemErrors.length))}"><b>存在 system_errors（${escapeHtml(String(item.systemErrors.length))}）</b> 机器结果不完整，不能当通过，也不进可信分母。${escapeHtml(JSON.stringify(item.systemErrors).slice(0, 800))}</div>`);
  }
  if (!included && !statusBad) {
    alerts.push(`<div class="judge-alert warn" data-judge-policy="${escapeHtml(policy.reason || "")}"><b>${escapeHtml(policy.reasonLabel || "不计分")}</b> 该判定保留展示，但不进入 Judge 语义评分分母。</div>`);
  }
  const roles = item.roles || [];
  const aggregation = item.aggregation || null;
  const unresolvedIds = (aggregation && aggregation.unresolved_issue_ids) || [];
  const disputedIds = (aggregation && aggregation.disputed_issue_ids) || [];
  const insufficient = roles.flatMap((role) => role.findings || []).filter((finding) => finding.kind === "insufficient_evidence");
  if (unresolvedIds.length) {
    alerts.push(`<div class="judge-alert warn" data-judge-unresolved="${escapeHtml(unresolvedIds.join(","))}"><b>存在未决评审项（${unresolvedIds.length}）</b> ${escapeHtml(unresolvedIds.join("、"))} 仍为 unresolved；不能把它当作已解决，也不能据此声称通过。</div>`);
  }
  if (insufficient.length) {
    alerts.push(`<div class="judge-alert warn" data-judge-insufficient="${insufficient.length}"><b>存在证据不足/无效证据项（${insufficient.length}）</b> 已按 insufficient_evidence 展示，未被程序静默当作通过。</div>`);
  }
  const roleHtml = roles.map((role) => {
    const findings = role.findings || [];
    const findingHtml = findings.map((finding) => {
      const refs = finding.evidenceRefs || [];
      return `
        <div class="judge-finding ${escapeHtml(role.verdict)}">
          <div class="judge-finding-summary">${escapeHtml(finding.summary)}</div>
          <div class="judge-finding-meta">
            ${finding.criterionId ? `<span class="tag">criterion ${escapeHtml(finding.criterionId)}</span>` : ""}
            ${finding.turnScope != null ? `<span class="tag">turn ${escapeHtml(String(finding.turnScope))}</span>` : ""}
            ${finding.judgment != null ? `<span class="tag">judgment ${escapeHtml(String(finding.judgment))}</span>` : ""}
            <span class="tag">${escapeHtml(finding.kind)}</span>
            <span class="tag">severity ${escapeHtml(finding.severity || "unknown")}</span>
            ${finding.suggestedLayer ? `<span class="tag">修复层 ${escapeHtml(finding.suggestedLayer)}</span>` : ""}
          </div>
          ${refs.map((ref) => `<blockquote class="judge-evidence"><code>${escapeHtml(ref.id)}</code>\n${escapeHtml(ref.quote)}</blockquote>`).join("")}
        </div>`;
    }).join("");
    return `
      <div class="judge-role">
        <div class="judge-role-head">
          <strong>${escapeHtml(role.role)}</strong>
          <span class="tag ${role.verdict === "passed" ? "green" : role.verdict === "failed" ? "red" : "amber"}">${escapeHtml(judgeVerdictLabel(role.verdict))}</span>
          ${findings.length ? `<span class="tag">${findings.length} findings</span>` : ""}
        </div>
        <div class="judge-role-reason">单 focus 角色建议，不等同于 case 总裁决；case verdict 由程序按 criterion 聚合。</div>
        ${role.reason ? `<div class="judge-role-reason">${escapeHtml(role.reason)}</div>` : ""}
        ${findingHtml || `<div class="judge-role-reason">该角色无 findings / 原文证据。</div>`}
      </div>`;
  }).join("");
  const issueRows = aggregation && Array.isArray(aggregation.issues)
    ? aggregation.issues.map((issue) => {
      const judgments = (issue.judgments || []).map((judgment) => `${judgment.role}: ${judgment.judgment}${judgment.unbound ? " (unbound)" : ""}`).join(" · ");
      const unresolved = unresolvedIds.includes(issue.criterion_id);
      return `
        <div class="judge-issue ${unresolved ? "unresolved" : ""}">
          <div><code>${escapeHtml(issue.criterion_id || "—")}</code>
            <span class="tag">${escapeHtml(issue.focus || "—")}</span>
            <span class="tag">turn ${escapeHtml(String(issue.turn_scope ?? "—"))}</span>
            <span class="tag ${issue.final_judgment === "passed" ? "green" : issue.final_judgment === "failed" ? "red" : "amber"}">final ${escapeHtml(issue.final_judgment || "—")}</span>
            ${issue.arbitration_used ? `<span class="tag amber">arbitration</span>` : ""}
            ${unresolved ? `<span class="tag amber">unresolved</span>` : ""}
          </div>
          <div class="judge-issue-judgments">${escapeHtml(judgments || "无角色判断")}</div>
        </div>`;
    }).join("")
    : "";
  const aggregationHtml = aggregation ? `
    <div class="judge-arb" data-judge-aggregation="1">
      <div><b>程序聚合</b> source=${escapeHtml(aggregation.source || "—")} · case_verdict=${escapeHtml(aggregation.case_verdict || "—")} · issues=${escapeHtml(String((aggregation.issues || []).length))}</div>
      <div>跨维度 verdict 不一致默认不是 dispute：${aggregation.cross_focus_verdict_mismatch_is_not_dispute ? "是" : "否/未标"}；disputed=${escapeHtml(disputedIds.join("、") || "无")}；unresolved=${escapeHtml(unresolvedIds.join("、") || "无")}</div>
      <div class="judge-issue-list">${issueRows || "无 issue 明细"}</div>
    </div>` : `<div class="judge-arb">旧结果未提供 <code>aggregation</code>；不推断程序聚合依据。</div>`;
  const arb = item.arbitration || {};
  const arbDetails = arb.details && Object.keys(arb.details).length
    ? `<pre>${escapeHtml(JSON.stringify(arb.details, null, 2))}</pre>`
    : "";
  const provenance = item.provenance || {};
  const provenanceRows = [
    ["judge_run_id", item.judgeRunId],
    ["run_id", item.runId],
    ["case_hash", item.caseHash],
    ["input_hash", item.inputHash],
    ["dsh_version", provenance.dsh_version],
    ["model", provenance.model],
    ["prompt_version", provenance.prompt_version],
    ["workflow_version", provenance.workflow_version],
    ["created_at", item.createdAt || provenance.created_at],
    ["finished_at", item.finishedAt || provenance.finished_at],
  ].filter(([, value]) => value != null && value !== "");
  const provenanceExtras = Object.keys(provenance)
    .filter((key) => !["dsh_version", "model", "prompt_version", "workflow_version", "created_at", "finished_at"].includes(key))
    .map((key) => [key, typeof provenance[key] === "object" ? JSON.stringify(provenance[key]) : provenance[key]]);
  const limitations = item.limitations || [];
  body.innerHTML = `
    <div class="judge-body-inner">
      <div class="judge-readonly">
        <span class="tag green">只读</span>
        <span class="tag">${escapeHtml(judgeStatusLabel(item.status))}</span>
        <span class="tag ${included ? "green" : "amber"}" data-judge-denominator="${escapeHtml(policy.denominator || "excluded")}">${included ? "进入语义分母" : "不进语义分母"}</span>
        <span class="tag">run ${escapeHtml(data.runId || "—")}</span>
        <span class="tag">${item.historical ? "历史快照" : "当前 caseHash"}</span>
        <span class="hint-inline">不写人工通过/不通过，不覆盖用户备注</span>
      </div>
      ${alerts.join("")}
      <div class="judge-verdict-row">
        <span>Case 总体判定（程序聚合）</span>
        <span class="judge-verdict ${escapeHtml(item.verdict)}" data-judge-verdict="${escapeHtml(item.verdict)}">${escapeHtml(judgeVerdictLabel(item.verdict))}</span>
        <span class="tag">status ${escapeHtml(item.status)}</span>
        <span class="tag" data-judge-evidence-count="${escapeHtml(String(item.evidenceCount || 0))}">原文证据 ${escapeHtml(String(item.evidenceCount || 0))} 条</span>
        ${evidenceTrust && evidenceTrust.checked > 0 ? `<span class="tag ${evidenceTrust.unverified ? "amber" : "green"}" data-judge-quote-verified="${escapeHtml(String(evidenceTrust.verified))}/${escapeHtml(String(evidenceTrust.checked))}">本地可追溯 ${escapeHtml(String(evidenceTrust.verified))}/${escapeHtml(String(evidenceTrust.checked))}</span>` : ""}
        <span class="tag ${item.coverageComplete ? "green" : "amber"}" data-judge-coverage="${item.coverageComplete ? "complete" : (item.coveragePresent ? "incomplete" : "missing")}">coverage ${item.coverageComplete ? "完整" : (item.coveragePresent ? "不完整" : "缺失")}</span>
        ${item.workflowVersion ? `<span class="tag">workflow ${escapeHtml(item.workflowVersion)}</span>` : ""}
      </div>
      <div class="judge-section">
        <h4>覆盖与完整性（v2 coverage / counts）</h4>
        <div class="judge-arb" data-judge-coverage-detail="1">
          <div><b>coverage present=${item.coveragePresent ? "是" : "否"} · complete=${item.coverageComplete ? "是" : "否"}</b>${item.workflowVersion ? ` · workflow=${escapeHtml(item.workflowVersion)}` : ""}${item.legacyUnverified ? " · <span class=\"tag amber\">legacy_unverified</span>" : ""}</div>
          ${coverageCounts ? `<pre>${escapeHtml(JSON.stringify(coverageCounts, null, 2))}</pre>` : `<div>未提供 aggregation.counts。</div>`}
          ${(coverageMissing.length || coverageUnbound.length || coverageUncertain.length || coverageUnresolved.length)
            ? `<div>missing=${escapeHtml(coverageMissing.join("、") || "无")} · unbound=${escapeHtml(coverageUnbound.join("、") || "无")} · uncertain=${escapeHtml(coverageUncertain.join("、") || "无")} · unresolved=${escapeHtml(coverageUnresolved.join("、") || "无")}</div>`
            : `<div>coverage 未列出 missing/unbound/uncertain/unresolved。</div>`}
          ${coverage ? `<details><summary>原始 coverage</summary><pre>${escapeHtml(JSON.stringify(coverage, null, 2))}</pre></details>` : ""}
        </div>
      </div>
      <div class="judge-section">
        <h4>程序聚合依据（criterion / focus / turn）</h4>
        ${aggregationHtml}
      </div>
      <div class="judge-section">
        <h4>每角色单 focus 建议</h4>
        ${roleHtml || `<div class="judge-empty"><strong>没有角色结果</strong><div>roles 为空，不能据此当通过。</div></div>`}
      </div>
      <div class="judge-section">
        <h4>分歧仲裁</h4>
        <div class="judge-arb">
          <div><b>${arb.required ? "需要仲裁" : "无需仲裁"}</b>${arb.reason ? " · " + escapeHtml(arb.reason) : ""}</div>
          ${arbDetails}
        </div>
      </div>
      <div class="judge-section">
        <h4>限制</h4>
        ${limitations.length ? `<ul class="judge-limitations">${limitations.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>` : `<div class="judge-role-reason">未声明额外限制。</div>`}
      </div>
      <div class="judge-section">
        <h4>版本与来源绑定</h4>
        <div class="judge-provenance">
          ${provenanceRows.map(([key, value]) => `<b>${escapeHtml(key)}</b><code>${escapeHtml(String(value))}</code>`).join("")}
          ${provenanceExtras.map(([key, value]) => `<b>${escapeHtml(key)}</b><code>${escapeHtml(String(value))}</code>`).join("")}
        </div>
      </div>
      ${(item.errors || []).length ? `<div class="judge-section"><h4>错误</h4><ul class="judge-errors">${item.errors.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>` : ""}
    </div>`;
}

function renderHistory() {
  const box = $("#history-box");
  const list = $("#history-list");
  const items = state.historical || [];
  $("#history-count").textContent = String(items.length);
  if (!items.length) {
    list.innerHTML = `<div class="empty-inline">没有旧 caseHash / suiteRevision 的历史审核记录。</div>`;
    return;
  }
  list.innerHTML = items.map((item) => `
    <div class="history-item">
      <div>历史快照 · <strong>${escapeHtml(statusLabel(item.status))}</strong> · revision ${escapeHtml(String(item.revision))} · ${escapeHtml(fmtTime(item.updatedAt))}</div>
      <div>caseHash <code>${escapeHtml(shortHash(item.caseHash, 14))}</code> · suite ${escapeHtml(item.suiteRevision)} · ${escapeHtml(item.runId || "案例定义")}</div>
      <div>审核人 ${escapeHtml(item.reviewer || "—")}</div>
      <div class="history-note">${escapeHtml(item.notes || "（无备注）")}</div>
    </div>`).join("");
}

function renderReviewForm() {
  const scope = state.scope;
  const run = state.runId;
  const noRun = scope === "run_answer" && !run;
  $("#review-target-title").textContent = SCOPE_LABEL[scope] + (scope === "run_answer" && run ? " · 运行回答" : "");
  $("#review-target-sub").textContent = noRun
    ? "暂无运行结果；该 run 没有回答文本，不能把 expected 当作 actual。"
    : scope === "case_definition"
      ? "独立保存为案例定义审核；不与运行回答共用状态。"
      : `独立保存为运行回答审核${run ? " · " + run : ""}；不拿 expected 当 actual。`;

  const status = state.selectedStatus;
  document.querySelectorAll(".status-btn").forEach((button) => {
    button.classList.toggle("selected", button.dataset.status === status);
    button.setAttribute("aria-pressed", button.dataset.status === status ? "true" : "false");
    button.disabled = noRun;
  });
  $("#save-btn").disabled = state.saving || !state.selectedStatus || noRun;
  $("#dirty-dot").hidden = !state.dirty;

  const record = state.review;
  if (record) {
    $("#saved-line").textContent = `已保存记录 · revision ${record.revision} · ${fmtTime(record.updatedAt)} · 审核人 ${record.reviewer || "—"} · ${SCOPE_LABEL[record.reviewScope] || ""}`;
    if (!state.dirty && !state.conflict) setSaveState(`已保存记录 · ${fmtTime(record.updatedAt)}`, "saved");
  } else {
    $("#saved-line").textContent = "未审核 · 尚无保存记录";
    if (!state.dirty && !state.conflict) setSaveState("尚未保存", "idle");
  }
  if (state.dirty && !state.conflict) setSaveState("未保存 · 草稿已保留", "dirty");

  const conflict = $("#conflict");
  if (state.conflict) {
    conflict.hidden = false;
    $("#conflict-message").textContent = state.conflict.message || "服务端已有更新，本地输入保留。";
    setSaveState("冲突，未保存", "error");
  } else {
    conflict.hidden = true;
  }
  const draftNote = $("#draft-note");
  if (state.draftNotice) {
    draftNote.hidden = false;
    draftNote.textContent = state.draftNotice;
  } else {
    draftNote.hidden = true;
  }
  renderHistory();
}

function applyFormValues() {
  $("#notes").value = state.formNotes || "";
  $("#reviewer").value = state.formReviewer || state.meta.reviewerDefault || "local-user";
}

function applyReviewAndDraft(data) {
  state.review = data.current || null;
  state.historical = data.historical || [];
  const record = state.review;
  let values = {
    status: record ? record.status : "",
    notes: record ? (record.notes || "") : "",
    reviewer: record ? (record.reviewer || state.meta.reviewerDefault || "local-user") : (localStorage.getItem("mr-last-reviewer") || state.meta.reviewerDefault || "local-user"),
  };
  const draft = readDraft();
  state.draftNotice = "";
  state.dirty = false;
  if (draftDiffersFromRecord(draft, record)) {
    values = {
      status: draft.status || "",
      notes: draft.notes || "",
      reviewer: draft.reviewer || values.reviewer,
    };
    state.dirty = true;
    state.draftNotice = `已恢复本地未保存草稿（基础 revision ${draft.baseRevision || 0}，保存 ${fmtTime(draft.savedAt)}）。服务端记录不会被静默覆盖。`;
  } else if (draft && !draftDiffersFromRecord(draft, record)) {
    clearDraft();
  }
  state.selectedStatus = values.status;
  state.formNotes = values.notes;
  state.formReviewer = values.reviewer;
  state.conflict = null;
  applyFormValues();
  renderReviewForm();
}

async function refreshReviewAndRun() {
  const seq = ++state.loadSeq;
  // Never build a request from a stale detail while a different case is still
  // loading; the newer selectCase() will issue its own refresh.
  if (!state.detail || state.detail.case.id !== state.selectedCaseId) return;
  const id = state.detail.case.id;
  const scope = state.scope;
  const runId = scope === "run_answer" ? state.runId : null;
  const includeHoldout = state.detail.case.split === "holdout" ? "&include_holdout=1" : "";
  const reviewUrl = `/api/reviews?case_id=${encodeURIComponent(id)}&scope=${encodeURIComponent(scope)}&runId=${encodeURIComponent(runId || "")}${includeHoldout}`;
  const runUrl = state.runId
    ? `/api/cases/${encodeURIComponent(id)}/run?runId=${encodeURIComponent(state.runId)}${includeHoldout}`
    : null;
  const judgeUrl = `/api/cases/${encodeURIComponent(id)}/judge?runId=${encodeURIComponent(state.runId || "")}${includeHoldout}`;
  // Clear before awaiting so a previous case/run's judge card is never shown
  // while the new request is in flight.  The seq guard below drops any stale
  // response that resolves after a newer selection.
  state.judgeResult = null;
  state.judgeError = null;
  renderJudgePanel();
  try {
    const [reviewData, runData, judgeData] = await Promise.all([
      api(reviewUrl),
      runUrl ? api(runUrl) : Promise.resolve(null),
      api(judgeUrl).catch((error) => ({ __judgeError: error.message || "Judge 结果加载失败" })),
    ]);
    if (seq !== state.loadSeq) return;
    state.runResult = runData;
    if (judgeData && judgeData.__judgeError) {
      state.judgeResult = null;
      state.judgeError = judgeData.__judgeError;
    } else {
      state.judgeResult = judgeData;
      state.judgeError = null;
    }
    applyReviewAndDraft(reviewData);
    renderRunPanel();
    renderJudgePanel();
  } catch (error) {
    if (seq !== state.loadSeq) return;
    state.review = null;
    state.historical = [];
    state.runResult = null;
    state.judgeResult = null;
    state.judgeError = error.message || "Judge 结果加载失败";
    state.selectedStatus = "";
    state.formNotes = "";
    state.formReviewer = localStorage.getItem("mr-last-reviewer") || state.meta.reviewerDefault || "local-user";
    state.dirty = false;
    state.conflict = null;
    applyFormValues();
    renderReviewForm();
    renderRunPanel();
    renderJudgePanel();
    toast(error.message || "审核记录加载失败", "error");
  }
}

async function selectCase(caseId, { skipGuard = false, force = false } = {}) {
  if (!caseId) return;
  if (!skipGuard && !guardDirty()) return;
  if (!force && state.selectedCaseId === caseId && state.detail && state.detail.case.id === caseId) {
    paintSelection();
    return;
  }
  const previousId = state.selectedCaseId;
  state.selectedCaseId = caseId;
  paintSelection();
  syncHash();
  const seq = ++state.loadSeq;
  try {
    const includeHoldout = state.split === "holdout" ? "?include_holdout=1" : "";
    const detail = await api(`/api/cases/${encodeURIComponent(caseId)}${includeHoldout}`);
    if (seq !== state.loadSeq) return;
    state.detail = detail;
    state.runs = detail.runs || [];
    const requestedRun = state.scope === "run_answer" && state.pendingRunFromHash && state.runs.some((run) => run.runId === state.pendingRunFromHash)
      ? state.pendingRunFromHash
      : null;
    state.pendingRunFromHash = null;
    state.runId = requestedRun || detail.selectedRunId || detail.defaultRunId || null;
    if (state.scope === "run_answer" && !state.runId && state.runs.length) {
      state.runId = state.runs[0].runId;
    }
    state.review = null;
    state.historical = [];
    state.runResult = null;
    state.judgeResult = null;
    state.judgeError = null;
    state.conflict = null;
    state.draftNotice = "";
    state.formNotes = "";
    state.formReviewer = localStorage.getItem("mr-last-reviewer") || state.meta.reviewerDefault || "local-user";
    state.selectedStatus = "";
    state.dirty = false;
    renderCaseBase();
    applyFormValues();
    renderReviewForm();
    renderRunPanel();
    renderJudgePanel();
    paintSelection();
    syncHash();
    await refreshReviewAndRun();
  } catch (error) {
    if (seq !== state.loadSeq) return;
    // Failed selection must not leave the list highlighting a case that the
    // main panel never opened.  Revert to the previous real selection.
    state.selectedCaseId = previousId;
    paintSelection();
    syncHash();
    toast(error.message || "案例加载失败", "error");
  }
}

async function selectRun(runId) {
  if (!runId || runId === state.runId) return;
  if (!guardDirty()) {
    renderRunPanel();
    return;
  }
  state.runId = runId;
  state.dirty = false;
  state.conflict = null;
  state.judgeResult = null;
  state.judgeError = null;
  syncHash();
  renderRunPanel();
  renderJudgePanel();
  await refreshReviewAndRun();
}

async function switchScope(scope) {
  if (scope === state.scope) return;
  if (!guardDirty()) return;
  state.scope = scope;
  state.dirty = false;
  state.conflict = null;
  state.draftNotice = "";
  if (scope === "run_answer" && !state.runId && state.runs.length) {
    state.runId = state.runs[0].runId;
  }
  syncHash();
  renderScopeCards();
  // Holdout stays hidden until the explicit exposure flow; switching the
  // review object must never silently load or select a holdout case.
  if (state.split === "holdout" && !state.holdoutExposed) {
    showHoldoutGate(true);
    clearWorkspace("留出集默认隐藏");
    $("#empty-state").querySelector("p").textContent = "请先点击左侧“查看留出集并记录曝光”。查看不会运行留出集。";
    return;
  }
  // Keep the filter/scope policy consistent: if the current case is still
  // visible, reload it for the new scope; if it is now hidden, select the
  // first visible case (or clear) so highlight and main panel cannot diverge.
  await loadList({ autoSelect: false });
  const visible = state.selectedCaseId && state.rows.some((row) => row.caseId === state.selectedCaseId);
  if (visible) {
    await selectCase(state.selectedCaseId, { skipGuard: true, force: true });
  } else if (state.rows.length) {
    const target = scope === "run_answer"
      ? (state.rows.find((row) => row.hasRuns) || state.rows[0])
      : state.rows[0];
    await selectCase(target.caseId, { skipGuard: true });
  } else {
    clearWorkspace("当前筛选没有匹配的案例");
  }
}

// Prominent entry from the empty workspace: open a case that actually has a
// run and land directly on the actual-answer scope.
async function enterRunAnswer() {
  if (state.split === "holdout" && !state.holdoutExposed) {
    toast("留出集仍需先在左侧显式查看并记录曝光。", "error");
    return;
  }
  if (!guardDirty()) return;
  state.scope = "run_answer";
  state.dirty = false;
  state.conflict = null;
  state.draftNotice = "";
  syncHash();
  renderScopeCards();
  await loadList({ autoSelect: false });
  if (!state.rows.length) {
    clearWorkspace("当前筛选没有匹配的案例");
    return;
  }
  if (state.selectedCaseId && state.rows.some((row) => row.caseId === state.selectedCaseId)) {
    await selectCase(state.selectedCaseId, { skipGuard: true, force: true });
    return;
  }
  const target = state.rows.find((row) => row.hasRuns) || state.rows[0];
  await selectCase(target.caseId, { skipGuard: true });
}

async function saveReview() {
  if (state.saving || !state.detail) return;
  if (!state.selectedStatus) {
    toast("请先选择：通过 / 不通过 / 存疑。", "error");
    return;
  }
  if (state.scope === "run_answer" && !state.runId) {
    toast("该 case 没有可选择 run，不能保存运行回答审核。", "error");
    return;
  }
  state.saving = true;
  $("#save-btn").disabled = true;
  setSaveState("保存中…", "idle");
  const payload = {
    caseId: state.detail.case.id,
    caseHash: state.detail.caseHash,
    suiteRevision: state.detail.suiteRevision,
    reviewScope: state.scope,
    runId: state.scope === "run_answer" ? state.runId : null,
    status: state.selectedStatus,
    notes: $("#notes").value,
    reviewer: $("#reviewer").value.trim() || state.meta.reviewerDefault || "local-user",
    expectedRevision: state.review ? Number(state.review.revision || 0) : 0,
  };
  // Capture the target + draft key before awaiting.  A slow response must be
  // applied only to the case/scope/run it was created for.
  const savedDraftKey = draftKey();
  const saveTarget = {
    caseId: payload.caseId,
    caseHash: payload.caseHash,
    scope: payload.reviewScope,
    runId: payload.runId,
  };
  const stillOnTarget = () => state.selectedCaseId === saveTarget.caseId
    && Boolean(state.detail)
    && state.detail.case.id === saveTarget.caseId
    && state.detail.caseHash === saveTarget.caseHash
    && state.scope === saveTarget.scope
    && (saveTarget.scope === "case_definition" || state.runId === saveTarget.runId);
  const formMatchesPayload = () => stillOnTarget()
    && $("#notes").value === payload.notes
    && state.selectedStatus === payload.status
    && ($("#reviewer").value.trim() || state.meta.reviewerDefault || "local-user") === payload.reviewer;
  try {
    const data = await api("/api/reviews", { method: "POST", body: JSON.stringify(payload) });
    state.saving = false;
    const targetStillOpen = stillOnTarget();
    const formUnchanged = formMatchesPayload();
    // Clear only the draft for the saved case; never B's draft when B was
    // selected while A's save was in flight.
    clearDraftKeyIfSaved(savedDraftKey, payload);
    if (!targetStillOpen || !formUnchanged) {
      if (formUnchanged) localStorage.setItem("mr-last-reviewer", payload.reviewer);
      if (targetStillOpen) {
        // User kept editing the same case while saving: keep the newer local
        // input and only advance the revision base.
        state.review = data.record;
        state.dirty = true;
      }
      renderReviewForm();
      $("#save-btn").disabled = state.saving || !state.selectedStatus || reviewBlockedByNoRun();
      await refreshListOnly();
      paintSelection();
      toast(`已保存${SCOPE_LABEL[saveTarget.scope]}审核（revision ${data.record.revision}）；当前选择未被旧回调覆盖。`, "success");
      return;
    }
    state.review = data.record;
    state.selectedStatus = data.record.status;
    state.formNotes = data.record.notes || "";
    state.formReviewer = data.record.reviewer || payload.reviewer;
    state.dirty = false;
    state.conflict = null;
    state.draftNotice = "";
    localStorage.setItem("mr-last-reviewer", state.formReviewer);
    $("#dirty-dot").hidden = true;
    $("#save-btn").disabled = false;
    $("#saved-line").textContent = `已保存记录 · revision ${data.record.revision} · ${fmtTime(data.record.updatedAt)} · 审核人 ${data.record.reviewer}`;
    setSaveState(`已保存 · ${fmtTime(data.record.updatedAt)} · revision ${data.record.revision}`, "saved");
    renderHistory();
    toast(`已保存${SCOPE_LABEL[state.scope]}审核（revision ${data.record.revision}）。`, "success");
    await refreshListOnly();
    paintSelection();
  } catch (error) {
    state.saving = false;
    if (!stillOnTarget()) {
      // A stale failure must not paint a conflict/error onto the new case.
      renderReviewForm();
      toast(`案例 ${saveTarget.caseId} 保存失败：${error.message || "未知错误"}`, "error");
      return;
    }
    $("#save-btn").disabled = !state.selectedStatus;
    if (error.status === 409) {
      state.conflict = {
        message: error.message,
        data: error.data,
        type: error.data && error.data.error,
      };
      renderReviewForm();
      toast("保存冲突：本地输入已保留，请查看冲突提示。", "error");
    } else {
      setSaveState("保存失败", "error");
      toast(error.message || "保存失败", "error");
    }
  }
}

async function reloadServerRecord() {
  if (!state.conflict) return;
  if (!window.confirm("这会放弃当前本地输入并加载服务端最新记录。确定继续？")) return;
  const key = draftKey();
  if (key) localStorage.removeItem(key);
  state.dirty = false;
  state.conflict = null;
  const id = state.selectedCaseId;
  await selectCase(id, { skipGuard: true, force: true });
}

async function revealHoldout() {
  if (!state.holdoutCount) return;
  try {
    const data = await api("/api/holdout/expose", {
      method: "POST",
      body: JSON.stringify({ reviewer: localStorage.getItem("mr-last-reviewer") || state.meta.reviewerDefault || "local-user" }),
    });
    state.holdoutExposed = true;
    const event = data.event || {};
    toast(data.first ? `已记录首次留出集曝光：${fmtTime(event.at)}` : `留出集此前已曝光：${fmtTime(event.at)}`, "success");
    if (state.split === "holdout") {
      await loadList({ autoSelect: true });
      if (!state.rows.length) clearWorkspace("留出集没有可显示案例");
    } else {
      await switchSplit("holdout", { skipGuard: true });
    }
  } catch (error) {
    toast(error.message || "曝光记录失败", "error");
  }
}

async function switchSplit(split, { skipGuard = false } = {}) {
  if (split === "holdout" && !state.holdoutCount) split = "development";
  if (split === state.split) {
    if (split === "holdout" && !state.holdoutExposed) showHoldoutGate(true);
    return;
  }
  if (!skipGuard && !guardDirty()) return;
  state.split = split;
  state.dirty = false;
  state.conflict = null;
  document.querySelectorAll(".split-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.split === split);
  });
  if (split === "holdout" && !state.holdoutExposed) {
    showHoldoutGate(true);
    clearWorkspace("留出集默认隐藏");
    $("#empty-state").querySelector("h2").textContent = "留出集默认隐藏";
    $("#empty-state").querySelector("p").textContent = "请先点击左侧“查看留出集并记录曝光”。查看不会运行留出集。";
    return;
  }
  await loadList({ autoSelect: true });
  if (!state.rows.length) {
    clearWorkspace("留出集没有可显示案例");
  }
}

function moveSelection(delta) {
  if (!state.rows.length) return;
  let index = state.rows.findIndex((row) => row.caseId === state.selectedCaseId);
  if (index < 0) index = 0;
  const next = state.rows[Math.max(0, Math.min(state.rows.length - 1, index + delta))];
  if (next && next.caseId !== state.selectedCaseId) selectCase(next.caseId);
}

function bindEvents() {
  $("#filter").addEventListener("change", async (event) => {
    if (!guardDirty()) {
      event.target.value = state.filter;
      return;
    }
    state.filter = event.target.value;
    state.dirty = false;
    await loadList({ autoSelect: true });
  });

  document.querySelectorAll(".split-tab").forEach((button) => {
    button.addEventListener("click", () => switchSplit(button.dataset.split));
  });
  document.querySelectorAll(".scope-card").forEach((button) => {
    button.addEventListener("click", () => switchScope(button.dataset.scope));
  });

  $("#case-list").addEventListener("click", (event) => {
    const item = event.target.closest("[data-case-id]");
    if (item) selectCase(item.dataset.caseId);
  });

  document.querySelectorAll(".status-btn").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedStatus = button.dataset.status;
      document.querySelectorAll(".status-btn").forEach((other) => {
        const selected = other.dataset.status === state.selectedStatus;
        other.classList.toggle("selected", selected);
        other.setAttribute("aria-pressed", selected ? "true" : "false");
      });
      $("#save-btn").disabled = reviewBlockedByNoRun();
      markDirty();
    });
  });

  $("#notes").addEventListener("input", markDirty);
  $("#reviewer").addEventListener("input", () => {
    localStorage.setItem("mr-last-reviewer", $("#reviewer").value);
    markDirty();
  });
  $("#save-btn").addEventListener("click", saveReview);
  $("#conflict-reload").addEventListener("click", reloadServerRecord);
  $("#prev-btn").addEventListener("click", () => moveSelection(-1));
  $("#next-btn").addEventListener("click", () => moveSelection(1));
  $("#holdout-reveal").addEventListener("click", revealHoldout);
  $("#empty-run-entry").addEventListener("click", enterRunAnswer);
  $("#export-json").addEventListener("click", () => downloadExport("json"));
  $("#export-csv").addEventListener("click", () => downloadExport("csv"));
  $("#hash-details").addEventListener("click", () => {
    const panel = $("#hash-panel");
    panel.hidden = !panel.hidden;
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveReview();
    } else if (event.altKey && event.key === "ArrowLeft") {
      event.preventDefault();
      moveSelection(-1);
    } else if (event.altKey && event.key === "ArrowRight") {
      event.preventDefault();
      moveSelection(1);
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  window.addEventListener("hashchange", () => {
    const params = parseHash();
    const wantedScope = params.get("scope") === "run_answer" ? "run_answer" : "case_definition";
    const wantedCase = params.get("case");
    const wantedRun = params.get("run");
    if (wantedScope !== state.scope) {
      state.scope = wantedScope;
      state.pendingRunFromHash = wantedRun;
      state.dirty = false;
      state.conflict = null;
      syncHash();
      renderScopeCards();
      if (state.split === "holdout" && !state.holdoutExposed) {
        showHoldoutGate(true);
        clearWorkspace("留出集默认隐藏");
        return;
      }
      if (wantedCase || state.selectedCaseId) {
        selectCase(wantedCase || state.selectedCaseId, { skipGuard: true, force: true });
      } else {
        loadList({ autoSelect: true });
      }
      return;
    }
    if (wantedCase && wantedCase !== state.selectedCaseId) {
      selectCase(wantedCase, { skipGuard: true });
    } else if (wantedScope === "run_answer" && wantedRun && wantedRun !== state.runId) {
      state.runId = wantedRun;
      syncHash();
      renderRunPanel();
      refreshReviewAndRun();
    }
  });
}

function downloadExport(format) {
  const anchor = document.createElement("a");
  anchor.href = `/api/export?format=${format}`;
  anchor.download = `manual-review.${format}`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  toast(`正在导出 ${format.toUpperCase()}（含留出集曝光记录）。`, "info");
}

async function init() {
  bindEvents();
  try {
    state.meta = await api("/api/meta");
  } catch (error) {
    toast("审核服务不可用：" + (error.message || "未知错误"), "error");
    return;
  }
  $("#suite-revision").textContent = state.meta.suite.revision || "—";
  $("#cases-sha").textContent = shortHash(state.meta.suite.casesSha256, 12);
  $("#cases-sha").title = state.meta.suite.casesSha256 || "";
  state.holdoutCount = Number((state.meta.suite && state.meta.suite.counts && state.meta.suite.counts.holdout) || 0);
  $("#holdout-count").textContent = String(state.holdoutCount);
  $("#holdout-tab").hidden = state.holdoutCount === 0;
  $("#holdout-reveal").hidden = state.holdoutCount === 0;
  state.holdoutExposed = Boolean(state.meta.holdout && state.meta.holdout.exposed);
  const savedReviewer = localStorage.getItem("mr-last-reviewer");
  $("#reviewer").value = savedReviewer || state.meta.reviewerDefault || "local-user";

  const params = parseHash();
  state.scope = params.get("scope") === "run_answer" ? "run_answer" : "case_definition";
  state.split = params.get("split") === "holdout" ? "holdout" : "development";
  state.pendingRunFromHash = params.get("run") || null;
  if (state.split === "holdout" && (!state.holdoutCount || !state.holdoutExposed)) {
    state.split = "development";
    syncHash();
  }
  document.querySelectorAll(".split-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.split === state.split);
  });
  renderScopeCards();
  await loadList({ autoSelect: false });
  const wanted = params.get("case");
  const visible = state.rows.find((row) => row.caseId === wanted);
  if (visible) {
    await selectCase(visible.caseId, { skipGuard: true });
  } else if (state.rows.length) {
    await selectCase(state.rows[0].caseId, { skipGuard: true });
  } else if (state.split === "holdout") {
    showHoldoutGate(!state.holdoutExposed);
    clearWorkspace("留出集默认隐藏");
  } else {
    clearWorkspace("当前筛选没有匹配的案例");
  }
  if (state.scope === "run_answer") {
    renderScopeCards();
  }
  state.ready = true;
  window.__reviewReady = true;
}

window.__reviewSnapshot = () => ({
  ready: state.ready,
  split: state.split,
  scope: state.scope,
  selectedCaseId: state.selectedCaseId,
  selectedId: state.selectedCaseId,
  activeCaseId: document.querySelector("#case-list .case-item.active")?.dataset.caseId || null,
  panelCaseId: $("#case-id") ? $("#case-id").textContent : null,
  caseHash: currentCaseHash(),
  suiteRevision: state.detail ? state.detail.suiteRevision : null,
  runId: state.runId,
  judgeAvailable: Boolean(state.judgeResult && state.judgeResult.available),
  judgeVerdict: state.judgeResult && state.judgeResult.selected ? state.judgeResult.selected.verdict : null,
  judgeStatus: state.judgeResult && state.judgeResult.selected ? state.judgeResult.selected.status : null,
  judgeSelectedRunId: state.judgeResult && state.judgeResult.selected ? state.judgeResult.selected.runId : null,
  judgeDenominator: state.judgeResult && state.judgeResult.selected && state.judgeResult.selected.scorePolicy
    ? state.judgeResult.selected.scorePolicy.denominator
    : null,
  judgeReviewTrust: state.judgeResult && state.judgeResult.selected && state.judgeResult.selected.scorePolicy
    ? state.judgeResult.selected.scorePolicy.reviewTrust
    : null,
  judgeCoverage: state.judgeResult && state.judgeResult.selected
    ? { present: Boolean(state.judgeResult.selected.coveragePresent), complete: Boolean(state.judgeResult.selected.coverageComplete) }
    : null,
  judgeSystemErrors: state.judgeResult && state.judgeResult.selected
    ? (state.judgeResult.selected.systemErrors || []).length
    : 0,
  judgeError: state.judgeError,
  judgePanelAfterRun: (() => {
    const run = $("#run-panel");
    const judge = $("#judge-panel");
    return Boolean(run && judge && run.nextElementSibling === judge);
  })(),
  status: state.selectedStatus,
  notes: $("#notes") ? $("#notes").value : "",
  reviewer: $("#reviewer") ? $("#reviewer").value : "",
  dirty: state.dirty,
  reviewRevision: state.review ? state.review.revision : null,
  conflict: state.conflict ? state.conflict.message : null,
  holdoutExposed: state.holdoutExposed,
  rows: state.rows.length,
});

init();
