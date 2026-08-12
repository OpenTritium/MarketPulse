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

function sentimentSpan(value, extraClass) {
  return el(
    "span",
    `sent ${sentimentClass(value)} ${extraClass ?? ""}`.trim(),
    sentimentText(value),
  );
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
    ["24h 均值", sentimentSpan(data.latest_24h, "value")],
    ["24h 事件数", el("div", "value", String(data.count_24h ?? 0))],
    ["7d 均值", sentimentSpan(data.avg_7d, "value")],
    ["7d 趋势", sentimentSpan(data.trend_7d, "value")],
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
  const when = event.published_at || event.first_seen_at;
  if (when) parts.push(fmtTime(when));
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
      ? `共 ${data.total} 个事件${state.hasMore ? "，点击下方「加载更多」" : ""}`
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
    container.append(meta);
    if (event.summary) container.append(el("p", "detail-summary", event.summary));
    if (event.related_symbols?.length) {
      const symbols = normalizeSymbols(event.related_symbols);
      const block = el("div", "detail-block");
      block.append(el("strong", null, "相关标的："));
      const tags = el("div", "sources");
      for (const s of symbols) {
        const tag = el("span", "tag", s.name);
        if (s.ts_code) {
          tag.classList.add("symbol-tag");
          tag.title = s.ts_code;
        }
        tags.append(tag);
      }
      block.append(tags);
      container.append(block);
      // 行情 K 线：因子块之前（有 ts_code 的标的）
      const withCode = symbols.filter((s) => s.ts_code);
      if (withCode.length) renderKlineSection(container, withCode, event);
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
  switchTo("search");
}

// ---- Tab 切换 ----

function switchTo(tab) {
  if (tab !== "search") $("search-input").value = "";
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

// ---- 行情 K 线（lightweight-charts）----

function normalizeSymbols(list) {
  // 数据已统一为 {name, ts_code, type} 对象格式（2026-08 回填），仅兜底非数组
  return Array.isArray(list) ? list : [];
}

let activeKlineChart = null;

// 分时刻度/十字线标签：UTC 秒 → 北京时间 HH:MM
function bjMinuteLabel(time) {
  const bj = new Date(time * 1000 + 8 * 3600 * 1000);
  const hh = String(bj.getUTCHours()).padStart(2, "0");
  const mm = String(bj.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function renderKlineSection(container, symbols, event) {
  const block = el("div", "detail-block");
  block.append(el("strong", null, "行情"));
  const tabs = el("div", "kline-tabs");
  // 周期切换：分时（当日 1min）优先——观察新闻后的即时走势
  const periods = [
    ["minute", "分时"],
    ["day", "日线"],
  ];
  const periodBtns = el("div", "kline-periods");
  let activePeriod = "minute";
  for (const [value, label] of periods) {
    const btn = el("button", `kline-period ${value === activePeriod ? "active" : ""}`, label);
    btn.type = "button";
    btn.addEventListener("click", () => {
      if (activePeriod === value) return;
      activePeriod = value;
      for (const b of periodBtns.children) b.classList.toggle("active", b === btn);
      if (current) loadKline(current);
    });
    periodBtns.append(btn);
  }
  const chartBox = el("div", "kline-box");
  block.append(tabs, periodBtns, chartBox);
  container.append(block);

  // 事件发生时刻（报道发布时间优先，采集时间兜底）→ K 线标记
  const eventTime = event?.published_at || event?.first_seen_at || null;
  const markerDate = eventTime ? eventTime.slice(0, 10) : null;
  // 事件时刻对齐到分钟（匹配分时 bar 的 trade_time 粒度），UTC 秒
  const markerMinuteTs = eventTime ? Math.floor(Date.parse(eventTime) / 60000) * 60 : null;

  let current = null;

  function loadKline(symbol) {
    if (activeKlineChart) {
      activeKlineChart.remove();
      activeKlineChart = null;
    }
    chartBox.replaceChildren(el("div", "muted", `加载 ${symbol.name} K 线…`));
    const granularity = activePeriod;
    const params = new URLSearchParams({
      ts_code: symbol.ts_code,
      granularity,
    });
    if (granularity === "day") params.set("days", "120");
    api(`/quote/kline?${params}`)
      .then((data) => {
        chartBox.replaceChildren();
        if (!data.kline?.length) {
          chartBox.append(el("div", "muted", "暂无行情数据"));
          return;
        }
        activeKlineChart = LightweightCharts.createChart(chartBox, {
          height: 280,
          layout: {
            background: { color: "transparent" },
            textColor: "#8b98a5",
            fontSize: 11,
          },
          grid: {
            vertLines: { color: "#1e2630" },
            horzLines: { color: "#1e2630" },
          },
          crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
          // 分时：刻度/十字线时间显示北京时间 HH:MM（默认格式是同一天重复的日期）
          localization: {
            timeFormatter: granularity === "minute" ? bjMinuteLabel : undefined,
          },
          timeScale: {
            borderColor: "#2a333d",
            tickMarkFormatter: granularity === "minute" ? bjMinuteLabel : undefined,
          },
          rightPriceScale: { borderColor: "#2a333d" },
        });
        const series = activeKlineChart.addSeries(LightweightCharts.CandlestickSeries, {
          upColor: "#e5484d",
          downColor: "#46a758",
          borderVisible: false,
          wickUpColor: "#e5484d",
          wickDownColor: "#46a758",
        });
        const fmtTime = (k) =>
          granularity === "minute"
            ? k.date // 后端已转 UTC 秒（UTCTimestamp）
            : k.date.replace(/(\d{4})(\d{2})(\d{2})/, "$1-$2-$3");
        series.setData(
          data.kline.map((k) => ({
            time: fmtTime(k),
            open: k.open,
            high: k.high,
            low: k.low,
            close: k.close,
          })),
        );
        activeKlineChart.timeScale().fitContent();
        // 事件发生时刻：蓝色箭头，随图表缩放/平移。
        // 分时：事件时刻对齐分钟（clamp 到数据范围）；日线：事件日期（超出落最新）
        if (markerDate) {
          const first = data.kline[0].date;
          const last = data.kline[data.kline.length - 1].date;
          const useMinute = granularity === "minute" && markerMinuteTs != null;
          const markerTime = useMinute
            ? Math.min(Math.max(markerMinuteTs, first), last)
            : markerDate >= last
              ? last
              : markerDate;
          const inRange = useMinute
            ? markerTime >= first && markerTime <= last
            : markerTime >= first && markerTime <= last;
          if (inRange) {
            LightweightCharts.createSeriesMarkers(series, [
              {
                time: markerTime,
                position: "aboveBar",
                color: "#4da3ff",
                shape: "arrowUp",
                text: "事件",
              },
            ]);
          }
        }
      })
      .catch((error) => {
        chartBox.replaceChildren(el("div", "muted", `行情加载失败：${error.message}`));
      });
  }

  for (const symbol of symbols) {
    const tab = el("button", "kline-tab", symbol.name);
    tab.type = "button";
    tab.addEventListener("click", () => {
      if (current === symbol) return;
      current = symbol;
      for (const t of tabs.children) t.classList.toggle("active", t === tab);
      loadKline(symbol);
    });
    tabs.append(tab);
  }
  // 默认加载第一个标的
  current = symbols[0];
  tabs.firstChild?.classList.add("active");
  loadKline(symbols[0]);
}
