const bootstrapToken = readBootstrapToken();

const state = {
  token: bootstrapToken || window.sessionStorage.getItem("observability_admin_token") || "",
  traces: [],
  selectedTraceId: null,
  selectedDetail: null,
  currentTimeline: null,
  currentGraph: null,
  currentArtifacts: null,
  activeStageId: null,
  summary: null,
  system: null,
  filter: "",
  live: true,
  loading: false,
  replayTimers: [],
};

const statusLabel = {
  ok: "正常",
  failed: "失败",
  warn: "警告",
  blocked: "阻断",
  skipped: "跳过",
  running: "运行中",
  unavailable: "不可用",
  stopped: "停止",
};

const el = {
  tokenForm: document.getElementById("token-form"),
  tokenInput: document.getElementById("admin-token"),
  refresh: document.getElementById("refresh"),
  status: document.getElementById("status"),
  live: document.getElementById("live"),
  replay: document.getElementById("replay"),
  speed: document.getElementById("replay-speed"),
  systemHealth: document.getElementById("system-health"),
  traceCount: document.getElementById("trace-count"),
  traceList: document.getElementById("trace-list"),
  filter: document.getElementById("filter"),
  traceTitle: document.getElementById("trace-title"),
  traceReason: document.getElementById("trace-reason"),
  traceMetrics: document.getElementById("trace-metrics"),
  pipeline: document.getElementById("pipeline"),
  stageTitle: document.getElementById("stage-title"),
  stageSummary: document.getElementById("stage-summary"),
  stageDetail: document.getElementById("stage-detail"),
  evidenceSummary: document.getElementById("evidence-summary"),
  evidenceList: document.getElementById("evidence-list"),
  graphSummary: document.getElementById("graph-summary"),
  graph: document.getElementById("trace-graph"),
};

function readBootstrapToken() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("admin_token") || "";
  if (!token) return "";
  window.sessionStorage.setItem("observability_admin_token", token);
  params.delete("admin_token");
  const query = params.toString();
  const nextUrl = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
  window.history.replaceState({}, "", nextUrl);
  return token;
}

function headers() {
  return state.token ? {"x-admin-token": state.token} : {};
}

async function api(path) {
  const response = await fetch(path, {headers: headers()});
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function loadTraces(options = {}) {
  if (state.loading) return;
  const quiet = Boolean(options.quiet && state.traces.length);
  const preserveStage = Boolean(options.preserveStage ?? quiet);
  state.loading = true;
  if (!quiet) {
    setStatus("正在加载观测数据...");
  }
  try {
    const [data, summary, system] = await Promise.all([
      api("/api/v2/observability/traces?limit=30"),
      api("/api/v2/observability/summary?limit=50"),
      api("/api/v2/observability/system"),
    ]);
    state.traces = data.items || [];
    state.summary = summary;
    state.system = system;
    renderSystemHealth();
    renderTraceList();
    if (!state.selectedTraceId && state.traces.length) {
      await selectTrace(state.traces[0].trace_id);
    } else if (state.selectedTraceId && state.traces.some((trace) => trace.trace_id === state.selectedTraceId)) {
      await selectTrace(state.selectedTraceId, {preserveStage});
    } else if (state.traces.length) {
      await selectTrace(state.traces[0].trace_id);
    } else {
      renderEmpty();
    }
    if (!quiet) {
      setStatus(summaryText());
    }
  } finally {
    state.loading = false;
  }
}

async function selectTrace(traceId, options = {}) {
  const previousTraceId = state.selectedTraceId;
  const previousStageId = state.activeStageId;
  state.selectedTraceId = traceId;
  const [detail, timeline, graph, artifacts] = await Promise.all([
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}`),
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}/timeline`),
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}/graph`),
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}/artifacts`),
  ]);
  state.selectedDetail = detail;
  state.currentTimeline = timeline;
  state.currentGraph = graph;
  state.currentArtifacts = artifacts;
  const stages = timeline.stages || [];
  const preservedStage = options.preserveStage && previousTraceId === traceId
    ? stages.find((stage) => stage.id === previousStageId)
    : null;
  const preferred = preservedStage
    || stages.find((stage) => stage.status === "failed")
    || stages.find((stage) => stage.status !== "skipped")
    || stages[0];
  state.activeStageId = preferred ? preferred.id : null;
  renderTraceList();
  renderTraceHeader();
  renderPipeline();
  renderStageDetail();
  renderGraph();
}

function renderSystemHealth() {
  const system = state.system || {};
  const processes = system.processes || {};
  const items = [
    ["FastAPI", system.fastapi?.status || "unknown", system.fastapi?.url || "-"],
    ["LM Studio", system.lm_studio?.status || "unknown", system.lm_studio?.loaded_models?.[0] || system.lm_studio?.message || "-"],
    ["Provider", system.provider?.status || "unknown", system.provider?.name || "-"],
    ["Tunnel", processes.cloudflared?.status || "unknown", processes.cloudflared?.pid ? `PID ${processes.cloudflared.pid}` : "-"],
    ["Reminder", processes.reminder_worker?.status || "unknown", processes.reminder_worker?.pid ? `PID ${processes.reminder_worker.pid}` : "-"],
  ];
  el.systemHealth.innerHTML = items.map(([title, status, value]) => `
    <div class="health-item">
      <span class="dot ${escapeHtml(status)}"></span>
      <span>
        <span class="health-title">${escapeHtml(title)} · ${escapeHtml(labelFor(status))}</span>
        <span class="health-value">${escapeHtml(value)}</span>
      </span>
    </div>
  `).join("");
}

function renderTraceList() {
  const query = state.filter.trim().toLowerCase();
  const traces = query ? state.traces.filter((trace) => traceSearchText(trace).includes(query)) : state.traces;
  el.traceCount.textContent = `${traces.length}`;
  el.traceList.innerHTML = "";
  for (const trace of traces) {
    const attrs = attrsOf(trace);
    const row = document.createElement("button");
    row.type = "button";
    row.className = `trace-row ${trace.trace_id === state.selectedTraceId ? "active" : ""}`;
    row.addEventListener("click", () => selectTrace(trace.trace_id).catch(showError));
    row.innerHTML = `
      <span class="pill ${escapeHtml(trace.status)}">${escapeHtml(labelFor(trace.status))}</span>
      <span>
        <span class="row-title">${escapeHtml(trace.workflow_type)}</span>
        <span class="row-sub">${escapeHtml(attrs.intent || trace.summary || trace.trace_id)}</span>
      </span>
      <span class="row-time">${escapeHtml(fmtTime(trace.started_at))}<br>${escapeHtml(fmtMs(trace.duration_ms))}</span>
    `;
    el.traceList.append(row);
  }
}

function renderTraceHeader() {
  const detail = state.selectedDetail;
  const timeline = state.currentTimeline;
  if (!detail || !timeline) return;
  const trace = detail.trace;
  const failedStage = (timeline.stages || []).find((stage) => stage.status === "failed");
  el.traceTitle.textContent = `${labelFor(trace.status)} · ${trace.workflow_type} · ${trace.trace_id}`;
  el.traceReason.textContent = failedStage
    ? `卡在「${failedStage.label}」：${failedStage.error || failedStage.summary || trace.summary}`
    : trace.summary || "这条消息已走完整条处理链路。";
  const attrs = attrsOf(trace);
  const items = [
    ["总耗时", fmtMs(trace.duration_ms)],
    ["关键阶段", fmtMs(timeline.critical_path_ms)],
    ["Provider", attrs.provider_name || "-"],
    ["Intent", attrs.intent || "-"],
    ["确认卡", attrs.confirmation_id || "-"],
  ];
  el.traceMetrics.innerHTML = items.map(([label, value]) => `
    <div class="metric"><span>${escapeHtml(label)}</span>${escapeHtml(value)}</div>
  `).join("");
}

function renderPipeline() {
  const stages = state.currentTimeline?.stages || [];
  el.pipeline.innerHTML = "";
  for (const stage of stages) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `stage-card ${stage.status} ${stage.id === state.activeStageId ? "active" : ""}`;
    card.dataset.stageId = stage.id;
    card.addEventListener("click", () => {
      state.activeStageId = stage.id;
      renderPipeline();
      renderStageDetail();
    });
    card.innerHTML = `
      <div class="stage-top">
        <span class="stage-label">${escapeHtml(stage.label)}</span>
        <span class="dot ${escapeHtml(stage.status)}"></span>
      </div>
      <div class="stage-status">${escapeHtml(labelFor(stage.status))} · ${escapeHtml(fmtMs(stage.duration_ms))}</div>
      <div>
        <p class="stage-desc">${escapeHtml(stage.description)}</p>
        <p class="stage-summary">${escapeHtml(stage.summary || "")}</p>
      </div>
      <div class="stage-progress"><span style="width:${Math.max(4, Math.min(100, stage.width_percent || 0))}%"></span></div>
    `;
    el.pipeline.append(card);
  }
}

function renderStageDetail() {
  const stage = activeStage();
  if (!stage) {
    el.stageTitle.textContent = "阶段详情";
    el.stageSummary.textContent = "选择一个阶段";
    el.stageDetail.innerHTML = "";
    el.evidenceSummary.textContent = "0 条";
    el.evidenceList.innerHTML = "";
    return;
  }
  el.stageTitle.textContent = `${stage.label} · ${labelFor(stage.status)}`;
  el.stageSummary.textContent = stage.error || stage.summary || stage.description;
  const spans = (state.selectedDetail?.spans || []).filter((span) => (stage.span_ids || []).includes(span.span_id));
  el.stageDetail.innerHTML = [
    stage.error ? `<div class="stage-detail-item"><strong>失败原因</strong><code>${escapeHtml(stage.error)}</code></div>` : "",
    `<div class="stage-detail-item"><strong>阶段说明</strong><span>${escapeHtml(stage.description)}</span></div>`,
    ...spans.map((span) => `
      <div class="stage-detail-item">
        <strong>${escapeHtml(span.name)} · ${escapeHtml(labelFor(span.status))} · ${escapeHtml(fmtMs(span.duration_ms))}</strong>
        <div class="json-box">${escapeHtml(JSON.stringify(span.attrs || {}, null, 2))}</div>
      </div>
    `),
  ].join("");
  renderEvidence(stage);
}

function renderEvidence(stage) {
  const artifacts = stage.artifacts || [];
  const events = stage.events || [];
  const diffs = stage.state_diffs || [];
  el.evidenceSummary.textContent = `${artifacts.length} 文物，${events.length} 事件，${diffs.length} 变更`;
  const blocks = [
    ...artifacts.map((item) => evidenceBlock("文物", item.kind, `${item.label} · ${item.redaction} · ${fmtBytes(item.size_bytes)}`)),
    ...events.map((item) => evidenceBlock("事件", item.name, item.message || item.level)),
    ...diffs.map((item) => evidenceBlock("状态", `${item.operation} ${item.entity_type}`, item.entity_id)),
  ];
  el.evidenceList.innerHTML = blocks.length ? blocks.join("") : "<div class=\"evidence-item\"><strong>无阶段证据</strong><span>本阶段没有关联 artifact/event/state diff。</span></div>";
}

function renderGraph() {
  const graph = state.currentGraph || {};
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  el.graphSummary.textContent = `${nodes.length} 节点`;
  const width = Math.max(420, el.graph.clientWidth || 420);
  const height = 430;
  el.graph.setAttribute("viewBox", `0 0 ${width} ${height}`);
  el.graph.innerHTML = "";
  if (!nodes.length) return;

  const positioned = nodes.map((node, index) => ({
    ...node,
    x: 24 + (index % 4) * Math.max(96, (width - 60) / 4),
    y: 40 + Math.floor(index / 4) * 64,
  }));
  const byId = new Map(positioned.map((node) => [node.id, node]));
  for (const edge of edges) {
    const from = byId.get(edge.from || edge.source);
    const to = byId.get(edge.to || edge.target);
    if (!from || !to) continue;
    el.graph.append(svg("path", {
      d: `M${from.x + 82},${from.y + 12} C${from.x + 112},${from.y + 12} ${to.x - 24},${to.y + 12} ${to.x},${to.y + 12}`,
      fill: "none",
      stroke: "#53606a",
      "stroke-width": "1.2",
    }));
  }
  for (const node of positioned) {
    const group = svg("g", {});
    const rect = svg("rect", {
      x: node.x,
      y: node.y,
      width: 92,
      height: 26,
      rx: 5,
      fill: statusColor(node.status),
      stroke: "#dce5ea33",
    });
    const text = svg("text", {
      x: node.x + 7,
      y: node.y + 17,
      fill: "#101214",
      "font-size": "10",
    });
    text.textContent = short(node.label, 13);
    group.append(rect, text);
    el.graph.append(group);
  }
}

function replayPipeline() {
  clearReplay();
  const cards = [...document.querySelectorAll(".stage-card")];
  if (!cards.length) return;
  const speed = Math.max(0.5, Number(el.speed.value || 1));
  cards.forEach((card) => card.classList.add("replay-hidden"));
  cards.forEach((card, index) => {
    state.replayTimers.push(window.setTimeout(() => card.classList.remove("replay-hidden"), index * 420 / speed));
  });
  state.replayTimers.push(window.setTimeout(() => setStatus("流程重播完成。"), cards.length * 420 / speed + 100));
  setStatus(`正在以 ${speed} 倍重播消息流程...`);
}

function activeStage() {
  return (state.currentTimeline?.stages || []).find((stage) => stage.id === state.activeStageId);
}

function evidenceBlock(type, title, body) {
  return `
    <div class="evidence-item">
      <strong>${escapeHtml(type)} · ${escapeHtml(title)}</strong>
      <code>${escapeHtml(body || "-")}</code>
    </div>
  `;
}

function renderEmpty() {
  clearReplay();
  el.traceTitle.textContent = "暂无消息记录";
  el.traceReason.textContent = "等待新的飞书或本地消息进入系统。";
  el.traceMetrics.innerHTML = "";
  el.pipeline.innerHTML = "";
  el.stageDetail.innerHTML = "";
  el.evidenceList.innerHTML = "";
  el.graph.innerHTML = "";
}

function traceSearchText(trace) {
  const attrs = attrsOf(trace);
  return [trace.status, trace.workflow_type, trace.summary, attrs.intent, attrs.provider_name].join(" ").toLowerCase();
}

function attrsOf(trace) {
  return trace && typeof trace.attrs === "object" ? trace.attrs : {};
}

function summaryText() {
  if (!state.summary) return `已加载 ${state.traces.length} 条消息记录。`;
  return `已加载 ${state.traces.length} 条消息记录。平均耗时 ${fmtMs(state.summary.avg_duration_ms)}，模型平均 ${fmtMs(state.summary.provider_latency_avg_ms)}，失败 ${state.summary.failed_trace_count} 次。`;
}

function setStatus(message, mode = "") {
  el.status.textContent = message;
  el.status.className = mode ? `status ${mode}` : "status";
}

function showError(error) {
  setStatus(error.message || String(error), "error");
}

function clearReplay() {
  state.replayTimers.forEach((timer) => window.clearTimeout(timer));
  state.replayTimers = [];
}

function labelFor(status) {
  return statusLabel[status] || status || "-";
}

function fmtMs(value) {
  if (value === null || value === undefined) return "-";
  return `${Math.round(Number(value))}ms`;
}

function fmtBytes(value) {
  if (value === null || value === undefined) return "-";
  if (value < 1024) return `${value}B`;
  return `${Math.round(value / 102.4) / 10}KB`;
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleTimeString();
}

function short(value, limit = 80) {
  const text = String(value ?? "");
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

function statusColor(status) {
  return {
    ok: "#4fbf7f",
    warn: "#d7a64a",
    failed: "#df6262",
    blocked: "#c776d9",
    skipped: "#697783",
    running: "#68a7ff",
  }[status] || "#68a7ff";
}

function svg(name, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

el.tokenInput.value = state.token;
el.live.checked = state.live;
el.tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.token = el.tokenInput.value.trim();
  window.sessionStorage.setItem("observability_admin_token", state.token);
  loadTraces().catch(showError);
});
el.refresh.addEventListener("click", () => loadTraces().catch(showError));
el.replay.addEventListener("click", replayPipeline);
el.live.addEventListener("change", () => {
  state.live = el.live.checked;
  setStatus(state.live ? "实时刷新已开启。" : "实时刷新已暂停。");
});
el.filter.addEventListener("input", () => {
  state.filter = el.filter.value;
  renderTraceList();
});

if (state.token) {
  loadTraces().catch(showError);
}

window.setInterval(() => {
  if (state.live && state.token) {
    loadTraces({quiet: true}).catch(showError);
  }
}, 2500);
