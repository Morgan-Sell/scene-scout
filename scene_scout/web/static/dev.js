/**
 * SceneScout Dev Section — observability panels.
 */

const devRunSelect = document.getElementById("dev-run-select");
const devAgentFilter = document.getElementById("dev-agent-filter");
const devLevelFilter = document.getElementById("dev-level-filter");
const devLogTable = document.getElementById("dev-log-table");
const devFeedMetrics = document.getElementById("dev-feed-metrics");
const devFeedTable = document.getElementById("dev-feed-table");
const devDryRunButton = document.getElementById("dev-dry-run-button");
const devDryRunPrompt = document.getElementById("dev-dry-run-prompt");
const devDryRunStatus = document.getElementById("dev-dry-run-status");
const devEmailPreview = document.getElementById("dev-email-preview");
const devHistoryTable = document.getElementById("dev-history-table");
const devCacheMetrics = document.getElementById("dev-cache-metrics");
const devCacheTable = document.getElementById("dev-cache-table");

let dryRunPollTimer = null;

function devEscapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function renderDevTable(headers, rows) {
  if (!rows.length) {
    return '<p class="empty-state">Nothing to show yet.</p>';
  }
  const head = headers.map((header) => `<th>${devEscapeHtml(header)}</th>`).join("");
  const body = rows
    .map(
      (row) =>
        `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`,
    )
    .join("");
  return `<table class="dev-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function formatPct(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  return `${Number(value).toFixed(1)}%`;
}

function formatTimestamp(value) {
  if (!value) {
    return "—";
  }
  return devEscapeHtml(value);
}

async function loadDevLogs() {
  const params = new URLSearchParams();
  if (devRunSelect.value) {
    params.set("run_id", devRunSelect.value);
  }
  if (devAgentFilter.value) {
    params.set("agent", devAgentFilter.value);
  }
  if (devLevelFilter.value) {
    params.set("level", devLevelFilter.value);
  }

  const response = await fetch(`/api/dev/logs?${params.toString()}`);
  const data = await response.json();
  if (!response.ok) {
    devLogTable.innerHTML = '<p class="empty-state">Could not load run logs.</p>';
    return;
  }

  const runs = data.runs || [];
  const selectedRunId = data.selected_run_id || "";
  devRunSelect.innerHTML = runs
    .map(
      (run) =>
        `<option value="${devEscapeHtml(run.run_id)}"${
          run.run_id === selectedRunId ? " selected" : ""
        }>${devEscapeHtml(run.run_id)} (${run.entry_count})</option>`,
    )
    .join("");

  const agents = new Set((data.entries || []).map((entry) => entry.agent));
  const currentAgent = devAgentFilter.value;
  devAgentFilter.innerHTML =
    '<option value="">All agents</option>' +
    [...agents]
      .sort()
      .map(
        (agent) =>
          `<option value="${devEscapeHtml(agent)}"${
            agent === currentAgent ? " selected" : ""
          }>${devEscapeHtml(agent)}</option>`,
      )
      .join("");

  const rows = (data.entries || []).map((entry) => [
    formatTimestamp(entry.timestamp),
    devEscapeHtml(entry.agent),
    devEscapeHtml(entry.level),
    devEscapeHtml(entry.message),
    `<pre class="dev-data">${devEscapeHtml(JSON.stringify(entry.data || {}, null, 2))}</pre>`,
  ]);
  devLogTable.innerHTML = renderDevTable(
    ["Time", "Agent", "Level", "Message", "Data"],
    rows,
  );
}

async function loadFeedHealth() {
  const response = await fetch("/api/dev/feed-health");
  const data = await response.json();
  if (!response.ok) {
    devFeedTable.innerHTML = '<p class="empty-state">Could not load feed health.</p>';
    return;
  }

  devFeedMetrics.innerHTML = `
    <p><strong>Latest run:</strong> ${devEscapeHtml(data.latest_run_id || "—")}</p>
    <p><strong>seen_entries hit rate:</strong> ${formatPct(data.seen_entries_hit_rate_pct)}</p>
    <p><strong>Post-date-filter yield:</strong> ${formatPct(data.post_date_filter_yield_pct)}</p>
  `;

  const rows = (data.feeds || []).map((feed) => [
    devEscapeHtml(feed.feed_name),
    feed.active ? "yes" : "no",
    devEscapeHtml(feed.status || "—"),
    feed.entries_fetched == null ? "—" : devEscapeHtml(String(feed.entries_fetched)),
    feed.etag_supported == null ? "—" : feed.etag_supported ? "yes" : "no",
    formatTimestamp(feed.last_fetch_at),
    devEscapeHtml(feed.error_message || "—"),
  ]);
  devFeedTable.innerHTML = renderDevTable(
    ["Feed", "Active", "Status", "Entries", "ETag", "Last fetch", "Error"],
    rows,
  );
}

async function loadHistory() {
  const response = await fetch("/api/dev/history?days=30");
  const data = await response.json();
  if (!response.ok) {
    devHistoryTable.innerHTML =
      '<p class="empty-state">Could not load recommendation history.</p>';
    return;
  }

  const rows = (data.entries || []).map((entry) => [
    formatTimestamp(entry.recommended_at),
    devEscapeHtml(entry.run_id),
    devEscapeHtml(String(entry.rank)),
    devEscapeHtml(entry.event_title),
    devEscapeHtml((entry.categories || []).join(", ") || "—"),
    formatPct(entry.score * 100),
    devEscapeHtml(entry.feedback_signal || "—"),
  ]);
  devHistoryTable.innerHTML = renderDevTable(
    ["Recommended", "Run", "Rank", "Event", "Categories", "Score", "Feedback"],
    rows,
  );
}

async function loadCacheInspection() {
  const response = await fetch("/api/dev/cache");
  const data = await response.json();
  if (!response.ok) {
    devCacheTable.innerHTML = '<p class="empty-state">Could not load cache stats.</p>';
    return;
  }

  const hitRates = data.enrichment_cache_hit_rates_pct || {};
  devCacheMetrics.innerHTML = `
    <p><strong>Latest run:</strong> ${devEscapeHtml(data.latest_run_id || "—")}</p>
    <p><strong>seen_entries hit rate:</strong> ${formatPct(data.seen_entries_hit_rate_pct)}</p>
    <p><strong>Performer cache:</strong> ${formatPct(hitRates.performer)}</p>
    <p><strong>Venue cache:</strong> ${formatPct(hitRates.venue)}</p>
    <p><strong>Vibe cache:</strong> ${formatPct(hitRates.vibe)}</p>
  `;

  const rows = (data.tables || []).map((table) => {
    if (table.cache_type === "venue_cache") {
      return [
        devEscapeHtml(table.cache_type),
        `geo ${table.ttl_days_geo}d / ctx ${table.ttl_days_context}d`,
        devEscapeHtml(String(table.rows)),
        devEscapeHtml(String(table.geo_active)),
        devEscapeHtml(String(table.context_active)),
        devEscapeHtml(String(table.geo_expired)),
        devEscapeHtml(String(table.context_expired)),
      ];
    }
    return [
      devEscapeHtml(table.cache_type),
      table.ttl_days == null ? "—" : `${table.ttl_days}d`,
      devEscapeHtml(String(table.rows ?? 0)),
      devEscapeHtml(String(table.active ?? 0)),
      devEscapeHtml(String(table.expired ?? 0)),
      "—",
      "—",
    ];
  });
  devCacheTable.innerHTML = renderDevTable(
    ["Cache", "TTL", "Rows", "Active", "Expired", "Geo expired", "Ctx expired"],
    rows,
  );
}

function showDryRunStatus(message, isError) {
  devDryRunStatus.hidden = false;
  devDryRunStatus.textContent = message;
  devDryRunStatus.className = isError ? "status-message error" : "status-message success";
}

async function pollDryRunStatus() {
  const response = await fetch("/api/dev/dry-run/status");
  const data = await response.json();
  if (!response.ok) {
    showDryRunStatus("Could not read dry-run status.", true);
    return;
  }

  if (data.status === "running") {
    showDryRunStatus("Dry-run in progress…", false);
    return;
  }

  clearInterval(dryRunPollTimer);
  dryRunPollTimer = null;
  devDryRunButton.disabled = false;

  if (data.status === "failed") {
    showDryRunStatus(data.error || "Dry-run failed.", true);
    devEmailPreview.hidden = true;
    return;
  }

  if (data.status === "completed") {
    showDryRunStatus(`Dry-run complete — run ${data.run_id}.`, false);
    if (data.run_id) {
      devEmailPreview.hidden = false;
      devEmailPreview.src = `/api/dev/dry-run/preview?run_id=${encodeURIComponent(data.run_id)}`;
    }
    await Promise.all([loadDevLogs(), loadFeedHealth(), loadCacheInspection(), loadHistory()]);
  }
}

async function startDryRun() {
  devDryRunButton.disabled = true;
  devEmailPreview.hidden = true;
  showDryRunStatus("Starting dry-run…", false);

  const body = {};
  if (devDryRunPrompt.value.trim()) {
    body.prompt = devDryRunPrompt.value.trim();
  }

  const response = await fetch("/api/dev/dry-run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    showDryRunStatus(data.error || "Could not start dry-run.", true);
    devDryRunButton.disabled = false;
    return;
  }

  if (data.status === "running") {
    dryRunPollTimer = setInterval(pollDryRunStatus, 2000);
    await pollDryRunStatus();
    return;
  }

  await pollDryRunStatus();
}

async function initDevSection() {
  if (!devRunSelect) {
    return;
  }

  devRunSelect.addEventListener("change", () => loadDevLogs());
  devAgentFilter.addEventListener("change", () => loadDevLogs());
  devLevelFilter.addEventListener("change", () => loadDevLogs());
  devDryRunButton.addEventListener("click", () => startDryRun());

  await Promise.all([
    loadDevLogs(),
    loadFeedHealth(),
    loadHistory(),
    loadCacheInspection(),
    pollDryRunStatus(),
  ]);
}

window.initDevSection = initDevSection;
