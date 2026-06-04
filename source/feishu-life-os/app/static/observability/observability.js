const state = {
  token: window.localStorage.getItem("observability_admin_token") || "",
  traces: [],
  selectedTraceId: null,
  selectedDetail: null,
  filter: "",
};

const lanes = ["ingest", "context", "model", "guard", "planner", "execute", "state", "external"];

const el = {
  tokenForm: document.getElementById("token-form"),
  tokenInput: document.getElementById("admin-token"),
  refresh: document.getElementById("refresh"),
  status: document.getElementById("status"),
  kpi: document.getElementById("kpi-strip"),
  traceCount: document.getElementById("trace-count"),
  traceList: document.getElementById("trace-list"),
  selectedTrace: document.getElementById("selected-trace"),
  timeline: document.getElementById("timeline"),
  filter: document.getElementById("filter"),
  contextSummary: document.getElementById("context-summary"),
  contextLens: document.getElementById("context-lens"),
  detailSummary: document.getElementById("detail-summary"),
  spanDetail: document.getElementById("span-detail"),
  artifactSummary: document.getElementById("artifact-summary"),
  artifactList: document.getElementById("artifact-list"),
  graphSummary: document.getElementById("graph-summary"),
  graph: document.getElementById("trace-graph"),
};

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

function setStatus(message, mode = "") {
  el.status.textContent = message;
  el.status.className = mode ? `status ${mode}` : "status";
}

function fmtMs(value) {
  if (value === null || value === undefined) return "-";
  return `${value}ms`;
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

function attrsOf(trace) {
  return trace && typeof trace.attrs === "object" ? trace.attrs : {};
}

function traceSearchText(trace) {
  const attrs = attrsOf(trace);
  return [
    trace.status,
    trace.workflow_type,
    trace.summary,
    attrs.intent,
    attrs.provider_name,
    attrs.risk_level,
  ].join(" ").toLowerCase();
}

async function loadTraces() {
  setStatus("Loading traces...");
  const data = await api("/api/v2/observability/traces?limit=20");
  state.traces = data.items || [];
  renderTraceList();
  if (!state.selectedTraceId && state.traces.length) {
    await selectTrace(state.traces[0].trace_id);
  } else if (state.selectedTraceId) {
    await selectTrace(state.selectedTraceId);
  } else {
    renderEmpty();
  }
  setStatus(`Loaded ${state.traces.length} trace(s).`);
}

async function selectTrace(traceId) {
  state.selectedTraceId = traceId;
  const [detail, timeline, graph, artifacts] = await Promise.all([
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}`),
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}/timeline`),
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}/graph`),
    api(`/api/v2/observability/traces/${encodeURIComponent(traceId)}/artifacts`),
  ]);
  state.selectedDetail = detail;
  renderTraceList();
  renderKpi(detail);
  renderTimeline(timeline);
  renderContextLens(detail);
  renderArtifacts(artifacts);
  renderGraph(graph);
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
      <span class="pill ${trace.status}">${trace.status}</span>
      <span>
        <span class="row-title">${escapeHtml(trace.workflow_type)}</span>
        <span class="row-sub">${escapeHtml(attrs.intent || trace.summary || trace.trace_id)}</span>
      </span>
      <span class="row-time">${escapeHtml(fmtTime(trace.started_at))}<br>${escapeHtml(fmtMs(trace.duration_ms))}</span>
    `;
    el.traceList.append(row);
  }
}

function renderKpi(detail) {
  const trace = detail.trace;
  const attrs = attrsOf(trace);
  el.selectedTrace.textContent = trace.trace_id;
  const items = [
    ["trace_id", trace.trace_id],
    ["workflow", trace.workflow_type],
    ["status", trace.status],
    ["duration", fmtMs(trace.duration_ms)],
    ["capture", trace.capture_id || "-"],
    ["agent_run", trace.agent_run_id || "-"],
    ["provider", attrs.provider_name || "-"],
    ["model", attrs.model || "-"],
    ["intent", attrs.intent || "-"],
    ["confidence", attrs.confidence ?? "-"],
    ["capsules", attrs.capsule_count ?? "-"],
    ["confirmation", attrs.confirmation_id || "-"],
  ];
  el.kpi.innerHTML = items
    .map(([label, value]) => `<div class="kpi"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div></div>`)
    .join("");
}

function renderTimeline(data) {
  el.timeline.innerHTML = "";
  const sorted = [...(data.lanes || [])].sort((a, b) => lanes.indexOf(a.name) - lanes.indexOf(b.name));
  for (const lane of sorted) {
    const row = document.createElement("div");
    row.className = "lane";
    row.innerHTML = `<div class="lane-name">${escapeHtml(lane.name)}</div><div class="lane-track"></div>`;
    const track = row.querySelector(".lane-track");
    for (const span of lane.spans || []) {
      const bar = document.createElement("button");
      bar.type = "button";
      bar.className = `span-bar ${span.status}`;
      bar.style.left = `${span.offset_percent}%`;
      bar.style.width = `${span.width_percent}%`;
      bar.style.background = `var(--${lane.name}, var(--accent))`;
      bar.textContent = `${span.name} ${fmtMs(span.duration_ms)}`;
      bar.title = `${span.name} | ${span.status} | ${fmtMs(span.duration_ms)}`;
      bar.addEventListener("click", () => renderSpanDetail(span));
      track.append(bar);
    }
    el.timeline.append(row);
  }
}

function renderContextLens(detail) {
  const artifact = (detail.artifacts || []).find((item) => item.kind === "context_v2");
  if (!artifact) {
    el.contextSummary.textContent = "No context artifact";
    el.contextLens.innerHTML = "";
    return;
  }
  const payload = artifact.payload_json || {};
  const capsules = payload.capsules || [];
  el.contextSummary.textContent = `${capsules.length} capsule(s), ${payload.provider_request_bytes || "-"} bytes`;
  el.contextLens.innerHTML = capsules
    .map((capsule) => `
      <div class="capsule">
        <strong>${escapeHtml(capsule.domain || "-")}</strong>
        <span>${escapeHtml(capsule.capsule_id || "-")}</span>
        <span>${capsule.rendered ? "rendered" : "skipped"}</span>
        <span>${escapeHtml(`facts ${capsule.facts_kept || 0}/${capsule.facts_total || 0}`)}</span>
      </div>
    `)
    .join("");
}

function renderSpanDetail(span) {
  el.detailSummary.textContent = `${span.name} / ${span.status}`;
  el.spanDetail.textContent = JSON.stringify(span, null, 2);
}

function renderArtifacts(data) {
  const artifacts = data.artifacts || [];
  const diffs = data.state_diffs || [];
  const events = data.events || [];
  el.artifactSummary.textContent = `${artifacts.length} artifacts, ${diffs.length} diffs`;
  const artifactHtml = artifacts.map((item) => `
    <div class="artifact">
      <strong>${escapeHtml(item.kind)}</strong> <span>${escapeHtml(item.label)}</span><br>
      <code>${escapeHtml(item.redaction)} ${escapeHtml(item.payload_hash || "")} ${escapeHtml(fmtBytes(item.size_bytes))}</code>
    </div>
  `);
  const diffHtml = diffs.map((item) => `
    <div class="artifact">
      <strong>${escapeHtml(item.operation)}</strong> <span>${escapeHtml(item.entity_type)} ${escapeHtml(item.entity_id)}</span>
    </div>
  `);
  const eventHtml = events.map((item) => `
    <div class="artifact">
      <strong>${escapeHtml(item.level)}</strong> <span>${escapeHtml(item.name)} ${escapeHtml(item.message || "")}</span>
    </div>
  `);
  el.artifactList.innerHTML = [...artifactHtml, ...diffHtml, ...eventHtml].join("");
}

function renderGraph(graph) {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  el.graphSummary.textContent = `${nodes.length} nodes`;
  const width = Math.max(420, el.graph.clientWidth || 420);
  const height = 320;
  el.graph.setAttribute("viewBox", `0 0 ${width} ${height}`);
  el.graph.innerHTML = "";
  if (!nodes.length) return;

  const laneOrder = new Map(lanes.map((lane, index) => [lane, index]));
  const positioned = nodes.map((node, index) => {
    const laneIndex = laneOrder.has(node.lane) ? laneOrder.get(node.lane) : lanes.length;
    return {
      ...node,
      x: 38 + Math.min(width - 92, index * 78),
      y: 34 + (laneIndex % 8) * 34,
    };
  });
  const byId = new Map(positioned.map((node) => [node.id, node]));

  for (const edge of edges) {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) continue;
    const path = svg("path", {
      d: `M${from.x + 42},${from.y + 10} C${from.x + 68},${from.y + 10} ${to.x - 20},${to.y + 10} ${to.x},${to.y + 10}`,
      fill: "none",
      stroke: "#53606a",
      "stroke-width": "1.2",
    });
    el.graph.append(path);
  }
  for (const node of positioned) {
    const group = svg("g", {});
    const rect = svg("rect", {
      x: node.x,
      y: node.y,
      width: 84,
      height: 22,
      rx: 5,
      fill: statusColor(node.status),
      stroke: "#dce5ea33",
    });
    const text = svg("text", {
      x: node.x + 7,
      y: node.y + 15,
      fill: "#101214",
      "font-size": "10",
    });
    text.textContent = short(node.label, 12);
    group.append(rect, text);
    el.graph.append(group);
  }
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

function renderEmpty() {
  el.kpi.innerHTML = "";
  el.timeline.innerHTML = "";
  el.contextLens.innerHTML = "";
  el.artifactList.innerHTML = "";
  el.spanDetail.textContent = "";
  el.graph.innerHTML = "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function showError(error) {
  setStatus(error.message || String(error), "error");
}

el.tokenInput.value = state.token;
el.tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.token = el.tokenInput.value.trim();
  window.localStorage.setItem("observability_admin_token", state.token);
  loadTraces().catch(showError);
});
el.refresh.addEventListener("click", () => loadTraces().catch(showError));
el.filter.addEventListener("input", () => {
  state.filter = el.filter.value;
  renderTraceList();
});

if (state.token) {
  loadTraces().catch(showError);
}
