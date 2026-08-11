// Market Pulse 前端：原生 JS，无构建链。所有 /api/* 请求经 bun 反代同源转发。
// 渲染只用 DOM API + textContent，新闻文本属外部数据，杜绝 innerHTML XSS。

const $ = (id) => document.getElementById(id);

const state = {
  startingAfter: null,
  hasMore: false,
  loading: false,
};

async function api(path) {
  const response = await fetch(`/api${path}`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body?.error?.message ?? `请求失败（HTTP ${response.status}）`);
  }
  return body;
}

function sentimentClass(value) {
  if (value == null) return "neutral";
  if (value > 0.05) return "up";
  if (value < -0.05) return "down";
  return "neutral";
}

function sentimentText(value) {
  if (value == null) return "—";
  return value > 0 ? `+${value.toFixed(2)}` : value.toFixed(2);
}

function showError(message) {
  let banner = document.querySelector(".error-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.className = "error-banner";
    document.querySelector("main").prepend(banner);
  }
  banner.textContent = `⚠ ${message}`;
}

function clearError() {
  document.querySelector(".error-banner")?.remove();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function sentimentSpan(value) {
  return el("span", `sent ${sentimentClass(value)}`, sentimentText(value));
}

async function loadOverview() {
  const data = await api("/sentiment/overview");
  const cards = [
    ["24h 均值", sentimentSpan(data.latest_24h)],
    ["24h 事件数", el("div", "value", String(data.count_24h ?? 0))],
    ["7d 均值", sentimentSpan(data.avg_7d)],
    ["7d 趋势", sentimentSpan(data.trend_7d)],
  ];
  const container = $("overview");
  container.replaceChildren();
  for (const [label, valueNode] of cards) {
    const card = el("div", "card");
    card.append(el("div", "label", label), valueNode);
    container.append(card);
  }
  $("updated-at").textContent = data.generated_at
    ? `更新于 ${data.generated_at.replace("T", " ").replace("Z", " UTC")}`
    : "";
}

async function loadTimeseries() {
  const data = await api("/sentiment/timeseries?window=day&hours=168");
  const series = data.series.slice(-7);
  const maxAbs = Math.max(0.1, ...series.map((s) => Math.abs(s.avg_sentiment ?? 0)));
  const container = $("timeseries");
  container.replaceChildren();
  for (const s of series) {
    const value = s.avg_sentiment ?? 0;
    const height = Math.max(3, Math.round((Math.abs(value) / maxAbs) * 100));
    const bar = el("div", `bar ${sentimentClass(value)}`);
    bar.style.height = `${height}%`;
    bar.append(
      el("span", "bar-value", value.toFixed(2)),
      el("span", "bar-label", s.bucket.slice(5)),
    );
    container.append(bar);
  }
}

async function loadFactors() {
  const data = await api("/sentiment/factors?hours=168");
  const container = $("factors");
  container.replaceChildren();
  for (const f of data.factors) {
    const row = el("div", "row");
    row.append(el("strong", null, f.factor), sentimentSpan(f.avg));
    const factor = el("div", "factor");
    factor.append(
      row,
      el(
        "div",
        "desc",
        `${f.description} · 事件 ${f.events} · 覆盖率 ${(f.coverage * 100).toFixed(0)}%`,
      ),
    );
    container.append(factor);
  }
}

function renderEvent(event) {
  const card = el("div", "event");
  card.append(el("div", "title", event.title));
  const metaParts = [];
  if (event.first_seen_at) {
    metaParts.push(event.first_seen_at.replace("T", " ").replace("Z", " UTC"));
  }
  metaParts.push("· 情绪");
  const meta = el("div", "meta");
  meta.append(el("span", null, metaParts.join(" ")));
  meta.append(sentimentSpan(event.sentiment));
  const impact = event.impact != null ? ` · 影响 ${event.impact.toFixed(2)}` : "";
  meta.append(el("span", null, `${impact} · 报道 ${event.report_count ?? 1}`));
  card.append(meta);
  if (event.summary) card.append(el("div", "summary", event.summary));
  if (event.sources?.length) {
    const sources = el("div", "sources");
    for (const source of event.sources) sources.append(el("span", "tag", source));
    card.append(sources);
  }
  return card;
}

async function loadTimeline(reset = false) {
  if (state.loading) return;
  state.loading = true;
  $("load-more").disabled = true;
  try {
    if (reset) {
      state.startingAfter = null;
      $("timeline").replaceChildren();
    }
    const params = new URLSearchParams({ limit: "20" });
    if (state.startingAfter) params.set("starting_after", state.startingAfter);
    const data = await api(`/timeline?${params}`);
    state.hasMore = data.has_more;
    state.startingAfter = data.starting_after ?? null;
    const list = $("timeline");
    for (const event of data.events) list.append(renderEvent(event));
    $("timeline-status").textContent = data.total
      ? `共 ${data.total} 个事件${state.hasMore ? "，继续下拉加载" : ""}`
      : "暂无事件（等待首轮采集）";
    $("load-more").hidden = !state.hasMore;
  } finally {
    state.loading = false;
    $("load-more").disabled = false;
  }
}

async function doSearch() {
  const query = $("search-input").value.trim();
  if (!query) {
    $("search-results").hidden = true;
    return;
  }
  const data = await api(`/search?q=${encodeURIComponent(query)}&k=10`);
  $("search-query").textContent = `「${query}」`;
  const list = $("search-list");
  list.replaceChildren();
  if (!data.results.length) {
    list.append(el("div", "muted", "无结果"));
  } else {
    for (const result of data.results) list.append(renderEvent(result));
  }
  $("search-results").hidden = false;
}

async function refreshAll() {
  clearError();
  try {
    await Promise.all([loadOverview(), loadTimeseries(), loadFactors()]);
  } catch (error) {
    showError(error.message);
  }
}

$("refresh-btn").addEventListener("click", () => {
  refreshAll();
  loadTimeline(true);
});
$("load-more").addEventListener("click", () => loadTimeline(false));
$("search-btn").addEventListener("click", doSearch);
$("search-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") doSearch();
});

refreshAll();
loadTimeline(true);
setInterval(refreshAll, 60_000); // 情绪面板每分钟自动刷新
