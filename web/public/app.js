// Market Pulse 前端：nginx 托管 + /api 同源反代，无框架。
// 渲染只用 DOM API + textContent，杜绝 innerHTML XSS。

const $ = (id) => document.getElementById(id);

const state = {
  tab: "dashboard",
  startingAfter: null,
  hasMore: false,
  loading: false,
};

// ---- API ----

async function api(path) {
  const response = await fetch(`/api${path}`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body?.error?.message ?? `请求失败（HTTP ${response.status}）`);
  }
  return body;
}

// ---- 时间（北京时间 UTC+8 展示）----

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const s = d.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  return s.replace(/\//g, "-");
}

// ---- 情绪展示 ----

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

// ---- DOM 工具 ----

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function sentimentSpan(value) {
  return el("span", `sent ${sentimentClass(value)}`, sentimentText(value));
}

function showError(message) {
  let banner = document.querySelector(".error-banner");
  if (!banner) {
    banner = el("div", "error-banner");
    document.querySelector("main").prepend(banner);
  }
  banner.textContent = `⚠ ${message}`;
}

function clearError() {
  document.querySelector(".error-banner")?.remove();
}

// ---- 仪表盘 ----

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
  $("updated-at").textContent = data.generated_at ? `更新于 ${fmtTime(data.generated_at)}` : "";
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
    const factor = el("div", "factor");
    const row = el("div", "row");
    row.append(el("strong", null, f.factor), sentimentSpan(f.avg));
    factor.append(row);
    // 情绪条：宽度按 |值| 比例（正值红、负值绿），无数据置灰
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill");
    if (f.avg != null) {
      const width = Math.min(Math.abs(f.avg) * 50, 50);
      fill.style.width = `${width}%`;
      fill.classList.add(sentimentClass(f.avg));
    }
    track.append(fill);
    factor.append(track);
    factor.append(
      el(
        "div",
        "desc",
        `${f.description} · 事件 ${f.events} · 覆盖率 ${(f.coverage * 100).toFixed(0)}%`,
      ),
    );
    container.append(factor);
  }
}

async function refreshDashboard() {
  clearError();
  try {
    await Promise.all([loadOverview(), loadTimeseries(), loadFactors()]);
  } catch (error) {
    showError(error.message);
  }
}

// ---- 事件渲染（列表 + 详情共用）----

function renderEventMeta(event) {
  const parts = [];
  if (event.first_seen_at) parts.push(fmtTime(event.first_seen_at));
  parts.push("· 情绪");
  const meta = el("div", "meta");
  meta.append(el("span", null, parts.join(" ")));
  meta.append(sentimentSpan(event.sentiment));
  if (event.impact != null) {
    meta.append(
      el("span", null, ` · 影响 ${event.impact.toFixed(2)} · 报道 ${event.report_count ?? 1}`),
    );
  }
  return meta;
}

function renderEventSources(sources) {
  const box = el("div", "sources");
  for (const source of new Set(sources ?? [])) {
    box.append(el("span", "tag", source));
  }
  return box;
}

function renderEventCard(event, clickable = true) {
  const card = el("div", "event");
  card.append(el("div", "title", event.title));
  card.append(renderEventMeta(event));
  if (event.summary) card.append(el("div", "summary", event.summary));
  card.append(renderEventSources(event.sources));
  if (clickable) {
    card.classList.add("clickable");
    card.addEventListener("click", () => openDetail(event.id));
  }
  return card;
}

// ---- 时间线 ----

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
    for (const event of data.events) list.append(renderEventCard(event));
    $("timeline-status").textContent = data.total
      ? `共 ${data.total} 个事件${state.hasMore ? "，继续下拉加载" : ""}`
      : "暂无事件（等待首轮采集）";
    $("load-more").hidden = !state.hasMore;
  } finally {
    state.loading = false;
    $("load-more").disabled = false;
  }
}

// ---- 事件详情（含报道正文）----

async function openDetail(eventId) {
  clearError();
  try {
    const event = await api(`/events/${eventId}`);
    const container = $("event-detail");
    container.replaceChildren();

    container.append(el("h2", null, event.title));
    const meta = renderEventMeta(event);
    meta.append(el("span", null, ` · 首次 ${fmtTime(event.first_seen_at)}`));
    container.append(meta);
    if (event.summary) container.append(el("p", "detail-summary", event.summary));
    if (event.related_symbols?.length) {
      const block = el("div", "detail-block");
      block.append(el("strong", null, "相关标的："));
      const tags = el("div", "sources");
      for (const s of event.related_symbols) tags.append(el("span", "tag", s));
      block.append(tags);
      container.append(block);
    }
    if (event.factors && Object.keys(event.factors).length) {
      const block = el("div", "detail-block");
      block.append(el("strong", null, "因子"));
      const factors = el("div", "factors");
      for (const [name, value] of Object.entries(event.factors)) {
        const f = el("div", "factor");
        const row = el("div", "row");
        row.append(el("strong", null, name), sentimentSpan(value));
        f.append(row);
        factors.append(f);
      }
      block.append(factors);
      container.append(block);
    }

    container.append(el("h3", null, `报道（${event.reports?.length ?? 0} 篇）`));
    for (const report of event.reports ?? []) {
      const block = el("div", "report");
      const head = el("div", "report-head");
      head.append(
        el("span", "tag", report.source),
        el("span", null, ` ${fmtTime(report.published_at || report.collected_at)}`),
      );
      const link = el("a", null, report.title);
      link.href = report.url;
      link.target = "_blank";
      link.rel = "noopener";
      head.append(link);
      block.append(head);
      if (report.raw_text) block.append(el("p", "report-text", report.raw_text));
      container.append(block);
    }

    switchTo("detail");
  } catch (error) {
    showError(error.message);
  }
}

// ---- 搜索 ----

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
    for (const result of data.results) list.append(renderEventCard(result));
  }
  $("search-results").hidden = false;
}

// ---- Tab 切换 ----

function switchTo(tab) {
  state.tab = tab;
  $("tab-dashboard").classList.toggle("active", tab === "dashboard");
  $("tab-timeline").classList.toggle("active", tab === "timeline");
  $("view-dashboard").hidden = tab !== "dashboard";
  $("view-timeline").hidden = tab !== "timeline";
  $("view-detail").hidden = tab !== "detail";
  $("search-results").hidden = tab !== "search";
  if (tab === "timeline") {
    loadTimeline(true);
  } else if (tab === "dashboard") {
    refreshDashboard();
  }
}

// ---- 事件绑定 ----

$("tab-dashboard").addEventListener("click", () => switchTo("dashboard"));
$("tab-timeline").addEventListener("click", () => switchTo("timeline"));
$("back-btn").addEventListener("click", () => switchTo("timeline"));
$("refresh-btn").addEventListener("click", () => {
  if (state.tab === "dashboard") refreshDashboard();
  else if (state.tab === "timeline") loadTimeline(true);
});
$("load-more").addEventListener("click", () => loadTimeline(false));
$("search-btn").addEventListener("click", doSearch);
$("search-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") doSearch();
});

refreshDashboard();
setInterval(() => {
  if (state.tab === "dashboard") refreshDashboard();
}, 60_000);
