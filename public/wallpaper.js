/* Desk Tape — live market wallpaper renderer.
   One payload, four densities, zero dependencies. */

(() => {
  "use strict";

  const params = new URLSearchParams(location.search);
  const MODES = ["horizon", "board", "focus", "minimal"];
  const REFRESH_DEFAULT = 30;

  // `rotate=12` sets the base dwell; `rotate=board:0,horizon:12,focus:20`
  // overrides per layout. Board is static unless it is named explicitly.
  const rotateSpec = (() => {
    const byMode = {};
    let base = 0;
    for (const part of (params.get("rotate") || "0").split(",")) {
      const [key, value] = part.split(":").map((item) => item.trim());
      if (value === undefined) {
        base = Math.max(0, Number(key) || 0) * 1000;
      } else if (Number(value) >= 0) {
        byMode[key] = Number(value) * 1000;
      }
    }
    return { base, byMode };
  })();

  const chartRegistry = new WeakMap();
  const DEBUG = params.get("debug") === "1";

  const state = {
    candles: {},
    chartStyle: ["line", "candles", "tv"].includes((params.get("chart") || "tv").toLowerCase())
      ? (params.get("chart") || "tv").toLowerCase()
      : "tv",
    mode: MODES.includes(params.get("mode")) ? params.get("mode") : "horizon",
    hero: params.get("hero") || "",
    symbols: params.get("symbols") || "",
    refreshMs: Math.max(10, Number(params.get("refresh")) || REFRESH_DEFAULT) * 1000,
    rotateMs: rotateSpec.base,
    rotateByMode: rotateSpec.byMode,
    rotateOrder: (params.get("order") || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    rotateLock: false,
    lang: params.get("lang") === "zh" ? "zh" : "en",
    static: params.get("static") === "1",
    hud: params.get("hud") !== "0",
    data: null,
    focus: params.get("focus") || "",
    lastPrice: null,
    timer: null,
    clockTimer: null,
    tapeTimer: null,
    rotateTimer: null,
    rotatePaused: params.get("rotate") === "0" ? false : false,
    hintTimer: null,
    edit: false,
  };

  /* Layout: insets keep the design clear of desktop icons and widgets.
     `inset=top:80,right:320` or `inset=80,320,40,0` (top,right,bottom,left). */
  const INSET_EDGES = ["top", "right", "bottom", "left"];
  const parseInsets = (raw) => {
    const out = {};
    (raw || "").split(",").forEach((token, index) => {
      const [name, value] = token.split(":").map((item) => (item || "").trim());
      if (value === undefined) {
        if (INSET_EDGES[index] && Number(name)) out[INSET_EDGES[index]] = Number(name);
        return;
      }
      if (INSET_EDGES.includes(name) && Number(value)) out[name] = Number(value);
    });
    return out;
  };
  const BLOCK_NAMES = ["ribbon", "hero", "chart", "rail", "tape", "clocks", "sessions", "brand"];

  // Free-form widgets: the user adds instances, each one is a normal draggable
  // block whose anchor is keyed by its instance id.
  const WIDGET_TYPES = ["quote", "spark", "candles", "clock", "gauge", "watch"];

  function parseWidgets(raw) {
    try {
      const parsed = JSON.parse(raw || "[]");
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter((item) => item && item.type && WIDGET_TYPES.includes(item.type))
        .slice(0, 8)
        .map((item, index) => ({
          id: String(item.id || `${item.type}${index + 1}`).replace(/[^\w-]/g, ""),
          type: item.type,
          symbol: item.symbol || "",
          zone: item.zone || "Asia/Shanghai",
        }));
    } catch (error) {
      return [];
    }
  }

  const parseBlocks = (raw) => {
    if (!raw) return null;
    const wanted = new Set(
      raw
        .split(",")
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean),
    );
    return BLOCK_NAMES.filter((name) => wanted.has(name));
  };

  const parseAnchors = (raw) => {
    const out = {};
    (raw || "").split(";").forEach((chunk) => {
      const [name, pair] = chunk.split(":");
      if (!name || !pair) return;
      const [x, y, scale] = pair.split(",").map(Number);
      if (Number.isFinite(x) && Number.isFinite(y)) {
        const anchor = [x, y];
        if (Number.isFinite(scale) && scale > 0) anchor.push(scale);
        out[name.trim()] = anchor;
      }
    });
    return out;
  };

  const ZONES = (params.get("clocks") || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

  const layout = {
    blocks: parseBlocks(params.get("blocks")),
    anchors: parseAnchors(params.get("anchors") || localStorage.getItem("desktape:anchors") || ""),
    priority: ["default", "session", "order"].includes(params.get("priority"))
      ? params.get("priority")
      : "default",
    edit: params.get("edit") === "1",
    insets: parseInsets(params.get("inset")),
    zoom: Math.min(1.35, Math.max(0.7, Number(params.get("zoom")) || 1)),
    align: ["left", "center", "right"].includes(params.get("align")) ? params.get("align") : "left",
    zones: ZONES.length
      ? ZONES
      : ["America/New_York", "Europe/London", "Asia/Shanghai", "Asia/Tokyo"],
    widgets: parseWidgets(params.get("widgets") || localStorage.getItem("desktape:widgets")),
  };

  const $ = (id) => document.getElementById(id);

  /* Priority: `session` puts exchanges that are trading right now first, then
     24h products, then closed markets by time-to-open. `order` honours the
     explicit order string only. */
  function rankedQuotes() {
    const quotes = state.data?.quotes || [];
    if (layout.priority === "order" && state.rotateOrder.length) {
      const rank = new Map(state.rotateOrder.map((symbol, index) => [symbol, index]));
      return [...quotes].sort((a, b) => (rank.get(a.symbol) ?? 999) - (rank.get(b.symbol) ?? 999));
    }
    if (layout.priority !== "session") return quotes;
    const weight = (quote) => {
      const session = quote.session || {};
      if (session.open && session.key !== "always") return 0;
      if (session.open) return 1;
      return 2;
    };
    return [...quotes].sort((a, b) => {
      const delta = weight(a) - weight(b);
      if (delta) return delta;
      const aNext = a.session?.nextSeconds ?? Number.MAX_SAFE_INTEGER;
      const bNext = b.session?.nextSeconds ?? Number.MAX_SAFE_INTEGER;
      return aNext - bNext;
    });
  }

  function applyBlocks() {
    document.querySelectorAll("[data-block]").forEach((node) => {
      const name = node.dataset.block;
      const visible = !layout.blocks || layout.blocks.includes(name);
      node.hidden = !visible;
    });
    const statsWanted = !layout.blocks || layout.blocks.includes("stats");
    document.body.dataset.stats = statsWanted ? "1" : "0";
  }

  function pruneWidgetAnchors() {
    const live = new Set((layout.widgets || []).map((widget) => `w:${widget.id}`));
    Object.keys(layout.anchors).forEach((key) => {
      if (key.startsWith("w:") && !live.has(key)) delete layout.anchors[key];
    });
  }

  function applyAnchors() {
    const stage = document.querySelector(".stage");
    if (!stage) return;
    let anchored = 0;
    document.querySelectorAll("[data-block]").forEach((node) => {
      const anchor = layout.anchors[node.dataset.block];
      if (!anchor) {
        node.removeAttribute("data-anchored");
        node.style.position = "";
        node.style.left = "";
        node.style.top = "";
        node.style.bottom = "";
        node.style.transform = "";
        return;
      }
      anchored += 1;
      node.dataset.anchored = "1";
      node.style.position = "absolute";
      node.style.left = `${anchor[0]}%`;
      node.style.top = `${anchor[1]}%`;
      node.style.bottom = "auto";
      node.style.zIndex = "4";
      const scale = anchor[2] || 1;
      node.style.transformOrigin = "left top";
      node.style.transform = scale === 1 ? "" : `scale(${scale})`;
    });
    document.body.dataset.anchored = anchored ? "1" : "0";
    syncHandles();
  }

  const snapPercent = (value, step = 1) => Math.round(value / step) * step;

  function anchorString() {
    return Object.entries(layout.anchors)
      .map(([name, pair]) => `${name}:${pair.join(",")}`)
      .join(";");
  }

  function persistLocal() {
    try {
      localStorage.setItem("desktape:anchors", anchorString());
      localStorage.setItem("desktape:widgets", JSON.stringify(layout.widgets || []));
    } catch (error) {
      /* storage can be disabled; the host still saves it */
    }
  }

  function persistLayout() {
    persistLocal();
    const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.desktape;
    if (bridge) {
      try {
        bridge.postMessage(exportLayout());
      } catch (error) {
        /* ignore */
      }
    }
  }

  function syncHandles() {
    document.querySelectorAll("[data-block]").forEach((node) => {
      const has = node.querySelector(":scope > .block-handle");
      if (state.edit && !has) {
        const handle = el("div", "block-handle");
        handle.title = "拖动缩放";
        handle.addEventListener("pointerdown", (event) => {
          if (!state.edit || event.button !== 0) return;
          event.preventDefault();
          event.stopPropagation();
          const startX = event.clientX;
          const box = node.getBoundingClientRect();
          const base = box.width || 1;
          const startScale = (layout.anchors[node.dataset.block] || [0, 0, 1])[2] || 1;
          const move = (ev) => {
            const scale = Math.min(1.8, Math.max(0.45, startScale * ((box.width + ev.clientX - startX) / base)));
            const anchor = layout.anchors[node.dataset.block] || [0, 0];
            layout.anchors[node.dataset.block] = [anchor[0], anchor[1], Number(scale.toFixed(2))];
            applyAnchors();
          };
          const up = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
            persistLocal();
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", up);
        });
        node.appendChild(handle);
      } else if (!state.edit && has) {
        has.remove();
      }
    });
  }

  const PINNABLE = new Set(["ribbon", "hero", "stats", "chart", "rail"]);

  // Absolute positioning resolves against the nearest positioned ancestor, not
  // necessarily .stage, so every % is measured in that same box.
  function anchorSpace(node) {
    const parent = node.offsetParent || document.querySelector(".stage");
    return parent.getBoundingClientRect();
  }

  function pinBlocks() {
    const stage = document.querySelector(".stage");
    if (!stage) return;
    document.querySelectorAll("[data-block]").forEach((node) => {
      const name = node.dataset.block;
      if (!PINNABLE.has(name) || layout.anchors[name]) return;
      const box = node.getBoundingClientRect();
      if (!box.width || !box.height) return;
      const space = anchorSpace(node);
      const x = ((box.left - space.left) / space.width) * 100;
      const y = ((box.top - space.top) / space.height) * 100;
      layout.anchors[name] = [
        Number(Math.min(90, Math.max(0, x)).toFixed(1)),
        Number(Math.min(90, Math.max(0, y)).toFixed(1)),
      ];
    });
  }


  /* -------------------------------------------------------------- widgets */

  function widgetLayer() {
    let layer = $("widget-layer");
    if (!layer) {
      layer = el("div", "widget-layer");
      layer.id = "widget-layer";
      document.querySelector(".desk").appendChild(layer);
    }
    return layer;
  }

  function widgetQuote(symbol) {
    const quotes = (state.data && state.data.quotes) || [];
    return quotes.find((item) => item.symbol === symbol) || quotes[0] || null;
  }

  function buildWidget(widget) {
    const node = el("div", `widget widget-${widget.type}`);
    node.dataset.block = `w:${widget.id}`;
    node.dataset.widget = widget.id;
    node.dataset.widgetType = widget.type;
    node.dataset.symbol = widget.symbol || "";

    if (widget.type === "quote" || widget.type === "spark") {
      const quote = widgetQuote(widget.symbol);
      const head = el("div", "wg-head");
      head.appendChild(el("span", "wg-label", quote ? quote.label || quote.symbol : "—"));
      if (quote) head.appendChild(el("span", "wg-pct num", fmtPct(quote.changePct)));
      node.appendChild(head);
      if (!quote) {
        node.appendChild(el("div", "wg-empty", "NO DATA"));
        return node;
      }
      node.dataset.trend = trendOf(quote.changePct);
      node.appendChild(el("div", "wg-price num", fmtPrice(quote.price)));
      const spark = el("div", "wg-spark");
      node.appendChild(spark);
      return node;
    }

    if (widget.type === "clock") {
      const zone = widget.zone || "Asia/Shanghai";
      node.dataset.zone = zone;
      const city = zone.split("/").pop().replace(/_/g, " ").toUpperCase();
      node.appendChild(el("div", "wg-label", city));
      node.appendChild(el("div", "wg-time num", "--:--"));
      return node;
    }

    if (widget.type === "gauge") {
      const sentiment = (state.data && state.data.sentiment) || {};
      node.appendChild(el("div", "wg-label", T.globalRisk));
      node.appendChild(el("div", "wg-gauge-value", String(Math.round(sentiment.score ?? 0))));
      const band = el("div", "wg-gauge-band", String(sentiment.label || ""));
      band.dataset.band = String(sentiment.band || "").toLowerCase();
      node.appendChild(band);
      return node;
    }

    if (widget.type === "candles") {
      const quote = widgetQuote(widget.symbol);
      node.dataset.trend = trendOf(quote ? quote.changePct : 0);
      const head = el("div", "wg-head");
      head.appendChild(el("span", "wg-label", (quote && (quote.label || quote.symbol)) || widget.symbol));
      if (quote) head.appendChild(el("span", "wg-pct num", fmtPct(quote.changePct)));
      node.appendChild(head);
      node.appendChild(el("div", "wg-candles"));
      return node;
    }

    if (widget.type === "watch") {
      node.appendChild(el("div", "wg-label", T.watchlist || "WATCHLIST"));
      const list = el("div", "wg-watch");
      for (const quote of rankedQuotes().slice(0, 5)) {
        const row = el("div", "wg-watch-row");
        row.appendChild(el("span", "wg-watch-name", shortLabel(quote)));
        row.appendChild(el("span", "wg-watch-price num", fmtPrice(quote.price)));
        const pct = el("span", "wg-watch-pct num", fmtPct(quote.changePct));
        pct.dataset.trend = trendOf(quote.changePct);
        row.appendChild(pct);
        list.appendChild(row);
      }
      node.appendChild(list);
      return node;
    }

    return node;
  }

  function drawWidgetSpark(node, widget) {
    const host = node.querySelector(".wg-spark");
    if (!host) return;
    const quote = widgetQuote(widget.symbol);
    const series = (quote && quote.series) || [];
    if (series.length < 2) return;
    const big = widget.type === "spark";
    const width = host.clientWidth;
    const height = host.clientHeight;
    if (!width || !height) return;
    try {
      drawLine(host, series, {
        temporal: true,
        padLeft: 0,
        padRight: big ? 44 : 0,
        padTop: 8,
        padBottom: 8,
        showAxis: false,
        showDays: false,
        lastTag: big,
        ghost: 0.18,
      });
    } catch (error) {
      /* a failed spark must not take the widget down */
    }
  }

  function renderWidgets() {
    const layer = widgetLayer();
    // Widgets with no saved position start in a loose grid instead of stacking.
    (layout.widgets || []).forEach((widget, index) => {
      const key = `w:${widget.id}`;
      if (layout.anchors[key]) return;
      layout.anchors[key] = [6 + (index % 4) * 23, 12 + Math.floor(index / 4) * 30];
    });
    layer.replaceChildren();
    for (const widget of layout.widgets || []) {
      const node = buildWidget(widget);
      if (state.edit) {
        const remove = el("button", "wg-remove", "×");
        remove.type = "button";
        remove.title = "删除组件";
        remove.addEventListener("pointerdown", (event) => event.stopPropagation());
        remove.addEventListener("click", (event) => {
          event.stopPropagation();
          removeWidget(widget.id);
        });
        node.appendChild(remove);
      }
      layer.appendChild(node);
      drawWidgetSpark(node, widget);
      if (widget.type === "candles") renderCandleWidget(node, widget);
      makeDraggable(node);
    }
    applyAnchors();
    tickWidgetClocks();
  }

  function tickWidgetClocks() {
    document.querySelectorAll('.widget-clock').forEach((node) => {
      const zone = node.dataset.zone;
      const out = node.querySelector(".wg-time");
      if (!out || !zone) return;
      try {
        out.textContent = new Date().toLocaleTimeString("en-GB", {
          timeZone: zone,
          hour12: false,
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        });
      } catch (error) {
        out.textContent = "--:--";
      }
    });
  }

  function addWidget(type, symbol) {
    const taken = new Set((layout.widgets || []).map((item) => item.id));
    let index = 1;
    while (taken.has(`${type}${index}`)) index += 1;
    layout.widgets = [
      ...(layout.widgets || []),
      { id: `${type}${index}`, type, symbol: symbol || (state.data && state.data.hero && state.data.hero.symbol) || "" },
    ];
    renderWidgets();
    persistLayout();
  }

  function removeWidget(id) {
    layout.widgets = (layout.widgets || []).filter((item) => item.id !== id);
    delete layout.anchors[`w:${id}`];
    renderWidgets();
    persistLayout();
  }

  function renderEditTools() {
    const existing = $("editbar");
    if (!state.edit) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    const bar = el("div", "editbar");
    bar.id = "editbar";
    const typeOptions = [
      ["quote", "报价"],
      ["spark", "走势"],
      ["candles", "K线"],
      ["clock", "时钟"],
      ["gauge", "情绪"],
      ["watch", "自选"],
    ]
      .map(([value, label]) => `<option value="${value}">${label}</option>`)
      .join("");
    const symbols = ((state.data && state.data.quotes) || [])
      .map((quote) => {
        const option = document.createElement("option");
        option.value = String(quote.symbol || "");
        option.textContent = String(quote.label || quote.symbol || "");
        return option.outerHTML;
      })
      .join("");
    bar.innerHTML =
      '<span class="editbar-title">编辑布局 · 拖动模块，拖右下角缩放</span>' +
      `<select id="widget-type" class="editbar-select">${typeOptions}</select>` +
      `<select id="widget-symbol" class="editbar-select">${symbols}</select>` +
      '<button data-act="add">＋组件</button>' +
      '<button data-act="save">完成</button>' +
      '<button data-act="reset">重排</button>' +
      '<button data-act="exit">退出</button>';
    bar.addEventListener("change", (event) => event.stopPropagation());
    bar.addEventListener("click", (event) => {
      const action = event.target.dataset.act;
      if (action === "add") {
        const type = ($("widget-type") || {}).value || "quote";
        const symbol = ($("widget-symbol") || {}).value || "";
        addWidget(type, symbol);
      } else if (action === "save") {
        persistLayout();
        exitEdit(true);
      } else if (action === "reset") {
        layout.anchors = {};
        applyAnchors();
        persistLayout();
      } else if (action === "exit") {
        exitEdit(true);
      }
    });
    document.body.appendChild(bar);
  }

  function exportLayout() {
    return {
      anchors: layout.anchors,
      blocks: layout.blocks || BLOCK_NAMES,
      priority: layout.priority,
      insets: layout.insets,
      zoom: layout.zoom,
      align: layout.align,
      zones: layout.zones,
      widgets: layout.widgets,
      chart: state.chartStyle,
    };
  }

  function makeDraggable(node) {
    node.addEventListener("pointerdown", (event) => {
      if (!state.edit || event.button !== 0) return;
      event.preventDefault();
      const stage = anchorSpace(node);
      const box = node.getBoundingClientRect();
      const startX = event.clientX;
      const startY = event.clientY;
      const baseX = ((box.left - stage.left) / stage.width) * 100;
      const baseY = ((box.top - stage.top) / stage.height) * 100;
      const current = layout.anchors[node.dataset.block] || [baseX, baseY];
      const scale = current[2];
      node.classList.add("dragging");
      const move = (ev) => {
        const x = Math.min(94, Math.max(0, snapPercent(baseX + ((ev.clientX - startX) / stage.width) * 100)));
        const y = Math.min(94, Math.max(0, snapPercent(baseY + ((ev.clientY - startY) / stage.height) * 100)));
        layout.anchors[node.dataset.block] = scale ? [x, y, scale] : [x, y];
        applyAnchors();
      };
      const up = () => {
        node.classList.remove("dragging");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        persistLocal();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  }

  function applyCopy() {
    document.documentElement.lang = state.lang === "zh" ? "zh-Hans" : "en";
    const tapeLabel = $("tape-label");
    if (tapeLabel) tapeLabel.textContent = T.tape;
    const gaugeTitle = $("gauge-title");
    if (gaugeTitle) gaugeTitle.textContent = T.riskAppetite;
  }

  function showEditBanner() {
    renderEditTools();
    syncHandles();
  }

  function exitEdit(saved = false) {
    state.edit = false;
    document.body.dataset.edit = "0";
    if (!saved && layout.beforeEdit) {
      layout.anchors = layout.beforeEdit;
      applyAnchors();
    }
    layout.beforeEdit = null;
    renderEditTools();
    syncHandles();
    if (saved) persistLayout();
    const hint = $("hint");
    if (hint) hint.hidden = !/hud=1/.test(location.search);
  }

  function applyLayout() {
    const root = document.documentElement;
    for (const [edge, value] of Object.entries(layout.insets)) {
      root.style.setProperty(`--pad-${edge}`, `${value}px`);
    }
    root.style.setProperty("--zoom", String(layout.zoom));
    document.body.dataset.align = layout.align;
  }
  const SVG_NS = "http://www.w3.org/2000/svg";

  const COPY = {
    en: {
      live: "LIVE",
      connecting: "CONNECTING",
      lost: "FEED LOST · RETRY",
      stale: "STALE",
      tape: "LIVE TAPE",
      watchlist: "WATCHLIST",
      updated: "UPDATED",
      opens: "OPENS",
      closes: "CLOSES",
      noTape: "NO INTRADAY TAPE",
      riskAppetite: "RISK APPETITE",
      globalRisk: "GLOBAL RISK",
      prevClose: "PREV CLOSE",
      open: "OPEN",
      dayHigh: "DAY HIGH",
      dayLow: "DAY LOW",
      high52: "52W HIGH",
      low52: "52W LOW",
      market: "MARKET",
      series: "SERIES",
      asOf: "AS OF",
      auto: "AUTO",
      pause: "PAUSED",
      lock: "LOCKED",
      marketOpen: "OPEN",
      marketClosed: "CLOSED",
    },
    zh: {
      live: "实时",
      connecting: "连接中",
      lost: "断线 · 重试",
      stale: "数据滞后",
      tape: "实时行情",
      watchlist: "自选",
      updated: "更新",
      opens: "开盘",
      closes: "收盘",
      noTape: "无分时数据",
      riskAppetite: "风险偏好",
      globalRisk: "全球风险",
      prevClose: "前收",
      open: "开盘",
      dayHigh: "日内高",
      dayLow: "日内低",
      high52: "52周高",
      low52: "52周低",
      market: "市场",
      series: "数据源",
      asOf: "截至",
      auto: "轮换",
      pause: "已暂停",
      lock: "已锁定",
      marketOpen: "开盘",
      marketClosed: "收盘",
      fear: "恐惧",
      greed: "贪婪",
      neutral: "中性",
    },
  };
  const T = COPY[state.lang];

  /* ------------------------------------------------------------- helpers */

  const decimalsFor = (symbol, price) => {
    if (!Number.isFinite(price)) return 2;
    if (symbol === "^TNX" || symbol === "US10Y") return 3;
    const abs = Math.abs(price);
    if (abs >= 100) return 2;
    if (abs >= 10) return 2;
    return 4;
  };

  const fmtPrice = (value, decimals = 2) =>
    Number.isFinite(value)
      ? value.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
      : "—";

  const fmtPct = (value) => (Number.isFinite(value) ? `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(2)}%` : "—");

  const fmtChange = (value, decimals = 2) =>
    Number.isFinite(value) ? `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(decimals)}` : "—";

  const trendOf = (value) => (Number.isFinite(value) ? (value > 0.02 ? "up" : value < -0.02 ? "down" : "flat") : "flat");

  const arrowOf = (trend) => (trend === "up" ? "▲" : trend === "down" ? "▼" : "▬");

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };

  const svgEl = (tag, attrs = {}) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    return node;
  };

  let gradientCount = 0;

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function countdown(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours > 0 ? `${pad(hours)}:${pad(minutes)}:${pad(secs)}` : `${pad(minutes)}:${pad(secs)}`;
  }

  function niceStep(span, target) {
    const raw = span / Math.max(1, target);
    const magnitude = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const normalised = raw / magnitude;
    const stepped = normalised >= 5 ? 10 : normalised >= 2 ? 5 : normalised >= 1 ? 2 : 1;
    return stepped * magnitude;
  }

  /* --------------------------------------------------------------- charts */

  /**
   * Draw a horizontal market line into `container`.
   * points: [[t, value], ...] where t is an epoch second, or an index
   *         sequence for synthetic two-point series.
   */
  /* ------------------------------------------------------------- candles */

  function drawCandles(container, bars, options = {}) {
    container.replaceChildren();
    if (!Array.isArray(bars) || bars.length < 2) return;
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) return;
    const {
      padLeft = 8,
      padRight = 92,
      padTop = 16,
      padBottom = 34,
      showAxis = true,
      showDays = true,
      timeZone = "America/New_York",
      decimals = 2,
    } = options;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      preserveAspectRatio: "none",
      class: "hz-svg",
    });

    const innerW = Math.max(1, width - padLeft - padRight);
    const innerH = Math.max(1, height - padTop - padBottom);
    let low = Infinity;
    let high = -Infinity;
    for (const bar of bars) {
      low = Math.min(low, bar.l);
      high = Math.max(high, bar.h);
    }
    const span = Math.max(high - low, Math.abs(high) * 1e-4, 1e-6);
    low -= span * 0.06;
    high += span * 0.06;
    const yOf = (value) => padTop + ((high - value) / (high - low)) * innerH;
    const stepW = innerW / bars.length;
    const bodyW = Math.max(2, stepW * 0.7);

    if (showAxis) {
      const step = niceStep(high - low, 4);
      for (let value = Math.ceil(low / step) * step; value <= high; value += step) {
        const y = yOf(value);
        svg.appendChild(
          svgEl("line", {
            x1: padLeft,
            x2: width - padRight,
            y1: y,
            y2: y,
            stroke: "rgba(244,241,232,0.08)",
            "stroke-width": 1,
          }),
        );
        const label = svgEl("text", {
          x: padLeft,
          y: y - 4,
          fill: "rgba(244,241,232,0.34)",
          "font-size": 9,
          "font-family": "var(--mono)",
        });
        label.textContent = value.toFixed(decimals);
        svg.appendChild(label);
      }
    }

    bars.forEach((bar, index) => {
      const x = padLeft + stepW * (index + 0.5);
      const up = bar.c >= bar.o;
      const colour = up ? "var(--up)" : "var(--down)";
      svg.appendChild(
        svgEl("line", {
          x1: x,
          x2: x,
          y1: yOf(bar.h),
          y2: yOf(bar.l),
          stroke: colour,
          "stroke-width": 1,
          "stroke-opacity": 0.75,
        }),
      );
      const top = yOf(Math.max(bar.o, bar.c));
      const bottom = yOf(Math.min(bar.o, bar.c));
      svg.appendChild(
        svgEl("rect", {
          x: x - bodyW / 2,
          y: top,
          width: bodyW,
          height: Math.max(1, bottom - top),
          fill: colour,
          "fill-opacity": up ? 0.85 : 0.9,
        }),
      );
    });

    /* previous close: the honest reference for "am I up today" */
    if (showAxis) {
      const lastClose = bars[bars.length - 1].c;
      const tag = svgEl("g");
      const bg = svgEl("rect", {
        x: width - padRight + 4,
        y: yOf(lastClose) - 8,
        width: padRight - 10,
        height: 16,
        fill: "var(--accent)",
      });
      const text = svgEl("text", {
        x: width - padRight + 8,
        y: yOf(lastClose) + 3,
        fill: "#0a0b0d",
        "font-size": 9,
        "font-family": "var(--mono)",
      });
      text.textContent = lastClose.toFixed(decimals);
      tag.appendChild(bg);
      tag.appendChild(text);
      svg.appendChild(tag);
    }

    if (showDays) {
      let lastLabel = "";
      bars.forEach((bar, index) => {
        const label = new Date(bar.t * 1000)
          .toLocaleDateString("en-US", { timeZone, weekday: "short" })
          .toUpperCase();
        if (label === lastLabel) return;
        lastLabel = label;
        if (index === 0) return;
        const x = padLeft + stepW * (index + 0.5);
        const text = svgEl("text", {
          x,
          y: height - 8,
          fill: "rgba(244,241,232,0.3)",
          "font-size": 9,
          "font-family": "var(--mono)",
          "text-anchor": "middle",
        });
        text.textContent = label;
        svg.appendChild(text);
        if (index > 0) {
          svg.appendChild(
            svgEl("line", {
              x1: x,
              x2: x,
              y1: padTop,
              y2: height - padBottom,
              stroke: "rgba(244,241,232,0.05)",
              "stroke-width": 1,
            }),
          );
        }
      });
    }

    container.appendChild(svg);
  }

  async function candleData(symbol, bars = 84, range = "5D") {
    const key = `${symbol}|${range}|${bars}`;
    if (state.candles[key]) return state.candles[key];
    const query = new URLSearchParams({ symbol, range, bars: String(bars) });
    try {
      const response = await fetch(`/api/wallpaper/candles?${query.toString()}`, { cache: "no-store" });
      const payload = await response.json();
      state.candles[key] = (payload && payload.bars) || [];
    } catch (error) {
      state.candles[key] = [];
    }
    return state.candles[key];
  }

  async function renderCandleWidget(node, widget) {
    const host = node.querySelector(".wg-candles");
    if (!host) return;
    const timeZone = zoneOfSymbol(widget.symbol);
    if (state.chartStyle !== "candles") {
      const drawn = await renderTVChart(host, { symbol: widget.symbol }, {
        bars: 96,
        timeZone,
        compact: true,
        height: host.clientHeight || 150,
      });
      if (drawn) return;
    }
    const bars = await candleData(widget.symbol, 44, "5D");
    if (!bars.length) {
      host.textContent = "—";
      return;
    }
    drawCandles(host, bars, {
      padLeft: 0,
      padRight: 56,
      padTop: 10,
      padBottom: 22,
      showAxis: false,
      showDays: true,
      timeZone,
    });
  }

  function zoneOfSymbol(symbol) {
    const quote = widgetQuote(symbol);
    return (quote && quote.seriesTimezone) || "America/New_York";
  }

  /* ------------------------------------------- TradingView Lightweight Charts */

  const TV_SRC = "/vendor/lightweight-charts.standalone.production.js";
  let tvLoading = null;

  function loadTradingView() {
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    if (tvLoading) return tvLoading;
    tvLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = TV_SRC;
      script.async = true;
      script.onload = () => (window.LightweightCharts ? resolve(window.LightweightCharts) : reject(new Error("no lib")));
      script.onerror = () => reject(new Error("load failed"));
      document.head.appendChild(script);
    });
    return tvLoading;
  }

  /* bars come back stamped in New York wall time, so map them to a UTC clock
     that prints the exchange's own hours on the axis. */
  function wallClockSeconds(epochSeconds, timeZone) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).formatToParts(new Date(epochSeconds * 1000));
    const pick = (type) => Number(parts.find((part) => part.type === type).value);
    return Date.UTC(pick("year"), pick("month") - 1, pick("day"), pick("hour"), pick("minute")) / 1000;
  }

  async function renderTVChart(container, quote, options = {}) {
    if (!container) return false;
    container.replaceChildren();
    const bars = await candleData(quote.symbol, options.bars || 140, "5D");
    if (!bars.length) return false;
    let lib;
    try {
      lib = await loadTradingView();
    } catch (error) {
      return false;
    }
    const timeZone = options.timeZone || "America/New_York";
    const height = Math.max(90, options.height || Math.max(120, (container.clientHeight || 280) - 30));
    if (options.compact) container.style.height = `${height}px`;
    const chart = lib.createChart(container, {
      width: Math.max(80, container.clientWidth - 6),
      height,
      layout: {
        background: { type: "solid", color: "transparent" },
        textColor: "rgba(244,241,232,0.55)",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "rgba(244,241,232,0.04)" },
        horzLines: { color: "rgba(244,241,232,0.06)" },
      },
      rightPriceScale: {
        borderColor: "rgba(244,241,232,0.08)",
        borderVisible: !options.compact,
        visible: !options.compact,
        scaleMargins: { top: 0.16, bottom: 0.16 },
      },
      timeScale: {
        borderColor: "rgba(244,241,232,0.08)",
        timeVisible: !options.compact,
        secondsVisible: false,
        visible: !options.compact,
      },
      crosshair: { mode: 0 },
      handleScroll: false,
      handleScale: false,
    });
    const series = chart.addCandlestickSeries({
      upColor: "#00d46a",
      downColor: "#ff355d",
      borderVisible: false,
      wickUpColor: "rgba(0,212,106,0.8)",
      wickDownColor: "rgba(255,61,93,0.8)",
      priceLineVisible: false,
      lastValueVisible: !options.compact,
    });
    series.setData(
      bars.map((bar) => ({
        time: wallClockSeconds(bar.t, timeZone),
        open: bar.o,
        high: bar.h,
        low: bar.l,
        close: bar.c,
      })),
    );
    chart.timeScale().fitContent();
    chartRegistry.set(container, chart);
    return true;
  }

  function paintPriceChart(container, quote, lineOptions, candleOptions) {
    if (!container) return;
    const old = chartRegistry.get(container);
    if (old) {
      try {
        old.remove();
      } catch (error) {
        /* already gone */
      }
      chartRegistry.delete(container);
    }
    if (state.chartStyle === "tv") {
      renderTVChart(container, quote, candleOptions).then((ok) => {
        if (!ok) drawLine(container, (quote && quote.series) || [], lineOptions);
      });
      return;
    }
    if (state.chartStyle !== "candles") {
      drawLine(container, (quote && quote.series) || [], lineOptions);
      return;
    }
    candleData(quote.symbol, candleOptions.bars || 96, "5D").then((bars) => {
      if (!bars.length) {
        drawLine(container, (quote && quote.series) || [], lineOptions);
        return;
      }
      drawCandles(container, bars, candleOptions);
    });
  }

  function drawLine(container, points, options = {}) {
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height || points.length < 2) return;

    const {
      padLeft = 0,
      padRight = 110,
      padTop = 26,
      padBottom = 48,
      temporal = points[points.length - 1][0] > 1e9,
      ghost = false,
      baseline = null,
      grid = true,
      showDays = true,
      lastTag = true,
      showAxis = true,
      timeZone = "America/New_York",
    } = options;

    const innerW = Math.max(1, width - padLeft - padRight);
    const innerH = Math.max(1, height - padTop - padBottom);

    /* Closed-market time is collapsed, so five sessions sit shoulder to
       shoulder instead of leaving three quarters of the frame empty. The line
       stays continuous and honest: only traded time is drawn. */
    const xs = [];
    if (!temporal) {
      points.forEach((_, index) => xs.push(index));
    } else {
      const diffs = [];
      for (let index = 1; index < points.length; index += 1) {
        diffs.push(points[index][0] - points[index - 1][0]);
      }
      const sorted = [...diffs].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)] || 900;
      const maxGap = Math.max(median * 4, 3 * 3600);
      let shift = 0;
      let previous = points[0][0];
      xs.push(previous);
      for (let index = 1; index < points.length; index += 1) {
        const stamp = points[index][0];
        const step = stamp - previous;
        if (step > maxGap) shift += step - median;
        xs.push(stamp - shift);
        previous = stamp;
      }
    }

    const firstX = xs[0];
    const lastX = xs[xs.length - 1];
    const xOf = (value) => padLeft + innerW * ((value - firstX) / Math.max(1, lastX - firstX));

    const values = points.map((point) => point[1]);
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (Number.isFinite(baseline)) {
      min = Math.min(min, baseline);
      max = Math.max(max, baseline);
    }
    const span = max - min || Math.abs(max || 1) * 0.004;
    min -= span * 0.13;
    max += span * 0.13;
    const yOf = (value) => padTop + innerH * (1 - (value - min) / (max - min));

    const bottom = height - padBottom;
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
    const uid = `hz${++gradientCount}`;
    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#ffb000";
    const decimals = Math.abs(max) >= 1000 ? 0 : Math.abs(max) >= 10 ? 2 : 4;

    if (grid) {
      const group = svgEl("g", { class: "hz-grid" });
      for (let index = 0; index <= 3; index += 1) {
        const y = padTop + (innerH * index) / 3;
        group.appendChild(svgEl("line", { x1: padLeft, x2: width - padRight, y1: y, y2: y }));
        if (showAxis) {
          const value = max - ((max - min) * index) / 3;
          const label = svgEl("text", { x: padLeft + 8, y: y + 3.5 });
          label.textContent = value.toLocaleString("en-US", {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
          });
          group.appendChild(label);
        }
      }
      svg.appendChild(group);
    }

    if (showDays && temporal) {
      const dayGroup = svgEl("g", { class: "hz-day" });
      let lastLabelX = -Infinity;
      let previousKey = "";
      for (let index = 0; index < points.length; index += 1) {
        const day = new Date(points[index][0] * 1000);
        const key = day.toLocaleDateString("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" });
        if (key === previousKey) continue;
        previousKey = key;
        const x = xOf(xs[index]);
        if (x < 26) continue;
        dayGroup.appendChild(svgEl("line", { x1: x, x2: x, y1: padTop - 14, y2: bottom + 6 }));
        if (x - lastLabelX < 64) continue;
        lastLabelX = x;
        const label = svgEl("text", { x: x + 8, y: bottom - 6 });
        label.textContent = day.toLocaleDateString("en-US", { timeZone, weekday: "short" }).toUpperCase();
        dayGroup.appendChild(label);
      }
      svg.appendChild(dayGroup);
    }

    if (Number.isFinite(baseline)) {
      svg.appendChild(
        svgEl("line", {
          x1: padLeft,
          x2: width - padRight,
          y1: yOf(baseline),
          y2: yOf(baseline),
          stroke: "rgba(242,239,228,0.2)",
          "stroke-dasharray": "2 6",
          "vector-effect": "non-scaling-stroke",
        })
      );
      const tag = svgEl("text", { x: width - padRight - 34, y: yOf(baseline) - 6 });
      tag.textContent = "PREV";
      tag.setAttribute("fill", "rgba(242,239,228,0.3)");
      tag.setAttribute("font-size", "8");
      tag.setAttribute("letter-spacing", "0.18em");
      svg.appendChild(tag);
    }

    const path = points
      .map(([, value], index) => `${index ? "L" : "M"}${xOf(xs[index]).toFixed(1)},${yOf(value).toFixed(1)}`)
      .join(" ");

    if (!ghost) {
      const defs = svgEl("defs");
      const gradient = svgEl("linearGradient", { id: uid, x1: 0, y1: 0, x2: 0, y2: 1 });
      gradient.appendChild(svgEl("stop", { offset: "0%", "stop-color": accent, "stop-opacity": "0.22" }));
      gradient.appendChild(svgEl("stop", { offset: "58%", "stop-color": accent, "stop-opacity": "0.07" }));
      gradient.appendChild(svgEl("stop", { offset: "100%", "stop-color": accent, "stop-opacity": "0" }));
      defs.appendChild(gradient);
      svg.appendChild(defs);
      svg.appendChild(
        svgEl("path", {
          d: `${path} L${xOf(lastX).toFixed(1)},${bottom + 6} L${padLeft},${bottom + 6} Z`,
          fill: `url(#${uid})`,
          class: "hz-area",
        })
      );
    }
    svg.appendChild(svgEl("path", { d: path, class: ghost ? "hz-line ghost" : "hz-line" }));

    if (lastTag && !ghost) {
      const lastY = yOf(points[points.length - 1][1]);
      const lastPointX = xOf(lastX);
      svg.appendChild(svgEl("circle", { cx: lastPointX, cy: lastY, r: 9, class: "hz-last-ring" }));
      svg.appendChild(svgEl("circle", { cx: lastPointX, cy: lastY, r: 3.4, class: "hz-last" }));
      const text = points[points.length - 1][1].toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
      const boxW = text.length * 7.2 + 14;
      const tagGroup = svgEl("g", { class: "hz-tag" });
      tagGroup.appendChild(svgEl("rect", { x: width - padRight + 4, y: lastY - 9, width: boxW, height: 18, rx: 1 }));
      const tagText = svgEl("text", { x: width - padRight + 11, y: lastY + 4 });
      tagText.textContent = text;
      tagGroup.appendChild(tagText);
      svg.appendChild(tagGroup);
    }

    container.replaceChildren(svg);
    container.dataset.rendered = "1";
  }

  function drawSpark(container, points, cls = "hz-line") {
    const width = container.clientWidth || 120;
    const height = container.clientHeight || 40;
    if (points.length < 2) return;
    const values = points.map((point) => point[1]);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const step = width / (points.length - 1);
    const d = points
      .map(([, v], index) => `${index ? "L" : "M"}${(index * step).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`)
      .join(" ");
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
    svg.appendChild(svgEl("path", { d, class: cls, stroke: "currentColor", "stroke-width": 1.2 }));
    container.replaceChildren(svg);
  }

  /* -------------------------------------------------------------- render */

  function renderClock() {
    const now = new Date();
    $("clock-time").textContent = now.toLocaleTimeString("en-GB", { hour12: false });
    const date = now
      .toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" })
      .toUpperCase()
      .replace(/,/g, "");
    $("clock-date").textContent = date;
    renderZones();
  }

  const zoneLabel = (zone) => {
    const parts = zone.split("/");
    return (parts[parts.length - 1] || zone).replace(/_/g, " ").toUpperCase();
  };

  function renderZones() {
    const host = $("clock-zones");
    if (!host) return;
    const now = new Date();
    host.replaceChildren();
    for (const zone of layout.zones) {
      let text = "--:--";
      try {
        text = now.toLocaleTimeString("en-GB", {
          timeZone: zone,
          hour12: false,
          hour: "2-digit",
          minute: "2-digit",
        });
      } catch (error) {
        text = "--:--";
      }
      const node = el("span", "zone");
      node.appendChild(el("span", "zone-city", zoneLabel(zone)));
      node.appendChild(el("span", "zone-time num", text));
      host.appendChild(node);
    }
  }

  function renderSessions(sessions) {
    const host = $("sessions");
    host.replaceChildren();
    for (const session of sessions || []) {
      const node = el("div", "session");
      node.dataset.state = session.state;
      node.dataset.at = session.next?.at || "";
      node.dataset.kind = session.next?.kind || "";
      node.appendChild(el("span", "session-city", session.label));
      const row = el("div", "session-row");
      row.appendChild(el("span", "session-time", session.localTime || "—"));
      row.appendChild(el("span", "session-state", session.stateLabel || session.state));
      node.appendChild(row);
      const next = el("span", "session-next", `${session.next?.kind === "close" ? T.closes : T.opens} ${countdown(session.next?.seconds || 0)}`);
      node.appendChild(next);
      host.appendChild(node);
    }
  }

  function tickSessions() {
    const now = Date.now();
    document.querySelectorAll(".session").forEach((node) => {
      const at = Date.parse(node.dataset.at || "");
      const label = node.querySelector(".session-next");
      if (!label || !Number.isFinite(at)) return;
      const remaining = (at - now) / 1000;
      if (remaining <= 0) return;
      const verb = node.dataset.kind === "close" ? T.closes : T.opens;
      label.textContent = `${verb} ${countdown(remaining)}`;
    });
  }

  function renderHero(hero) {
    if (!hero) return;
    const decimals = decimalsFor(hero.symbol, hero.price);
    const trend = trendOf(hero.changePct);
    document.body.dataset.trend = trend;
    $("hero-label").textContent = hero.label || hero.symbol;
    $("hero-symbol").textContent = hero.symbol;
    $("hero-range").textContent = hero.seriesRange || (hero.seriesSource === "flat" ? "1D" : "5D");
    $("hero-currency").textContent = hero.currency && hero.currency !== "USD" ? hero.currency : "";

    const priceNode = $("hero-price");
    tweenNumber(priceNode, hero.price, decimals);
    priceNode.dataset.value = String(hero.price ?? "");

    $("hero-arrow").textContent = arrowOf(trend);
    $("hero-change").textContent = fmtChange(hero.change, decimals);
    $("hero-pct").textContent = fmtPct(hero.changePct);
    $("hero-asof").textContent = hero.asOf ? `${T.asOf} ${hero.asOf.slice(0, 10)}` : "";

    const stats = $("hero-stats");
    stats.replaceChildren();
    const rows = [
      [T.prevClose, fmtPrice(hero.previousClose, decimals)],
      [T.open, fmtPrice(hero.open, decimals)],
      [T.dayHigh, fmtPrice(hero.high, decimals)],
      [T.dayLow, fmtPrice(hero.low, decimals)],
      [T.high52, fmtPrice(hero.yearHigh, decimals)],
      [T.low52, fmtPrice(hero.yearLow, decimals)],
    ];
    for (const [label, value] of rows) {
      if (value === "—") continue;
      const cell = el("div");
      cell.appendChild(el("dt", null, label));
      cell.appendChild(el("dd", "num", value));
      stats.appendChild(cell);
    }

    paintPriceChart(
      $("horizon-chart"),
      hero,
      {
        ghost: hero.seriesSource === "flat",
        baseline: hero.previousClose,
        showDays: true,
        padRight: 118,
        padBottom: 44,
        timeZone: hero.seriesTimezone || "America/New_York",
      },
      {
        padLeft: 54,
        padRight: 118,
        padBottom: 40,
        timeZone: hero.seriesTimezone || "America/New_York",
        bars: 120,
      },
    );
  }

  function tweenNumber(node, value, decimals) {
    if (!Number.isFinite(value)) {
      node.textContent = "—";
      return;
    }
    const from = Number(node.dataset.value);
    if (!state.static && Number.isFinite(from) && from !== value) {
      const start = performance.now();
      const duration = 720;
      const fromText = fmtPrice(from, decimals);
      node.textContent = fromText;
      const step = (now) => {
        const progress = Math.min(1, (now - start) / duration);
        const eased = 1 - Math.pow(1 - progress, 3);
        node.textContent = fmtPrice(from + (value - from) * eased, decimals);
        if (progress < 1) requestAnimationFrame(step);
        else node.textContent = fmtPrice(value, decimals);
      };
      requestAnimationFrame(step);
      document.body.classList.add(value > from ? "flash-up" : "flash-down");
      window.setTimeout(() => document.body.classList.remove("flash-up", "flash-down"), 1400);
    } else {
      node.textContent = fmtPrice(value, decimals);
    }
    node.dataset.value = String(value);
  }

  function renderRibbon(quotes) {
    const host = $("ribbon");
    if (!host) return;
    host.replaceChildren();
    for (const quote of quotes || []) {
      const cell = el("div", "ribbon-cell");
      cell.dataset.trend = trendOf(quote.changePct);
      const head = el("div", "ribbon-head");
      head.appendChild(el("span", "ribbon-label", shortLabel(quote)));
      head.appendChild(el("span", "ribbon-pct num", fmtPct(quote.changePct)));
      cell.appendChild(head);
      const spark = el("div", "ribbon-spark");
      cell.appendChild(spark);
      host.appendChild(cell);
      if ((quote.series || []).length > 1) drawSpark(spark, quote.series, "ribbon-path");
    }
  }

  function shortLabel(quote) {
    const map = {
      "^GSPC": "S&P",
      "^IXIC": "NDQ",
      "^DJI": "DOW",
      "^VIX": "VIX",
      "^N225": "N225",
      "^HSI": "HSI",
      "^FTSE": "FTSE",
      "BTC-USD": "BTC",
      "GC=F": "GOLD",
      "CL=F": "WTI",
      "^TNX": "10Y",
      "DX-Y.NYB": "DXY",
      "EURUSD=X": "EUR",
      "USDJPY=X": "JPY",
    };
    return map[quote.symbol] || String(quote.label || quote.symbol || "").slice(0, 5).toUpperCase();
  }

  function renderGauge(sentiment) {
    const fill = $("gauge-fill");
    if (!sentiment || !Number.isFinite(sentiment.score)) {
      $("gauge-score").textContent = "—";
      $("gauge-band").textContent = "N/A";
      return;
    }
    const score = Math.max(0, Math.min(100, sentiment.score));
    fill.style.strokeDashoffset = String(100 - score);
    const warm = score >= 66 ? "var(--up)" : score >= 40 ? "var(--amber)" : "var(--down)";
    fill.style.stroke = warm;
    $("gauge-score").textContent = score.toFixed(1);
    const band = $("gauge-band");
    const bandKey = String(sentiment.band || "").toLowerCase();
    band.textContent =
      state.lang === "zh" && T[bandKey] ? T[bandKey] : sentiment.band || "—";
    band.style.color = warm;
    const gaugeLabel = (sentiment.label || "").toLowerCase();
    $("gauge-label").textContent =
      gaugeLabel && gaugeLabel !== "global risk" ? sentiment.label.toUpperCase() : T.globalRisk;
  }

  function renderMovers(quotes) {
    const host = $("movers");
    host.replaceChildren();
    const sorted = [...quotes].filter((q) => Number.isFinite(q.changePct)).sort((a, b) => Math.abs(b.changePct) - Math.abs(a.changePct));
    const limit = Math.max(...sorted.map((q) => Math.abs(q.changePct)), 1);
    for (const quote of sorted.slice(0, 5)) {
      const trend = trendOf(quote.changePct);
      const li = el("li");
      li.dataset.trend = trend;
      li.style.color = trend === "up" ? "var(--up)" : trend === "down" ? "var(--down)" : "var(--flat)";
      li.appendChild(el("span", "mover-name", quote.label || quote.symbol));
      li.appendChild(el("span", "mover-pct", fmtPct(quote.changePct)));
      const bar = el("div", "mover-bar");
      const fill = el("i");
      fill.style.width = `${Math.max(6, (Math.abs(quote.changePct) / limit) * 100)}%`;
      bar.appendChild(fill);
      li.appendChild(bar);
      host.appendChild(li);
    }
  }

  function renderBoard(payload) {
    const host = $("board");
    host.replaceChildren();
    const heroSymbol = payload.hero?.symbol;
    for (const quote of rankedQuotes()) {
      const trend = trendOf(quote.changePct);
      const decimals = decimalsFor(quote.symbol, quote.price);
      const tile = el("div", "tile");
      tile.dataset.trend = trend;
      tile.dataset.symbol = quote.symbol;
      tile.title = `${quote.label || quote.symbol} — click to pin`;
      if (quote.symbol === heroSymbol) tile.dataset.hero = "1";

      const head = el("div", "tile-head");
      head.appendChild(el("span", null, quote.label || quote.symbol));
      const session = quote.session || {};
      if (session.key && session.key !== "always") {
        const chip = el("span", "tile-session", session.open ? T.marketOpen : T.marketClosed);
        chip.dataset.open = session.open ? "1" : "0";
        head.appendChild(chip);
      }
      head.appendChild(el("span", "tile-symbol", quote.symbol));
      tile.appendChild(head);

      tile.appendChild(el("div", "tile-price num", fmtPrice(quote.price, decimals)));

      const foot = el("div", "tile-foot");
      foot.appendChild(el("span", "tile-chg num", fmtChange(quote.change, decimals)));
      foot.appendChild(el("span", "tile-pct num", fmtPct(quote.changePct)));
      tile.appendChild(foot);

      const spark = el("div", "tile-spark");
      tile.appendChild(spark);
      host.appendChild(tile);
      requestAnimationFrame(() => drawSpark(spark, quote.series || []));
    }
  }

  function renderFocus(payload) {
    const quotes = payload.quotes;
    const target = quotes.find((q) => q.symbol === state.focus) || payload.hero || quotes[0];
    if (!target) return;
    const decimals = decimalsFor(target.symbol, target.price);
    const trend = trendOf(target.changePct);

    const head = $("focus-head");
    head.replaceChildren();
    const name = el("div", "focus-name");
    name.appendChild(el("h1", null, target.label || target.symbol));
    name.appendChild(el("p", null, `${target.symbol}${target.name ? ` · ${target.name}` : ""}`));
    head.appendChild(name);
    head.appendChild(el("div", "focus-price num", fmtPrice(target.price, decimals)));
    const delta = el("div", "focus-delta");
    delta.dataset.trend = trend;
    delta.appendChild(el("b", "num", fmtPct(target.changePct)));
    delta.appendChild(el("span", "num", fmtChange(target.change, decimals)));
    head.appendChild(delta);

    paintPriceChart(
      $("focus-chart"),
      target,
      {
        ghost: target.seriesSource === "flat",
        baseline: target.previousClose,
        padLeft: 8,
        padRight: 132,
        padTop: 18,
        padBottom: 34,
        showDays: true,
        timeZone: target.seriesTimezone || "America/New_York",
      },
      {
        padLeft: 0,
        padRight: 132,
        padTop: 18,
        padBottom: 34,
        timeZone: target.seriesTimezone || "America/New_York",
        bars: 140,
      },
    );

    const stats = $("focus-stats");
    stats.replaceChildren();
    const rows = [
      [T.prevClose, fmtPrice(target.previousClose, decimals)],
      [T.open, fmtPrice(target.open, decimals)],
      [T.dayHigh, fmtPrice(target.high, decimals)],
      [T.dayLow, fmtPrice(target.low, decimals)],
      [T.high52, fmtPrice(target.yearHigh, decimals)],
      [T.low52, fmtPrice(target.yearLow, decimals)],
      [T.market, target.marketStatus || "—"],
      [T.series, (target.seriesSource || "—").toUpperCase()],
    ];
    for (const [label, value] of rows) {
      const row = el("div");
      row.appendChild(el("dt", null, label));
      row.appendChild(el("dd", "num", value));
      stats.appendChild(row);
    }

    const switcher = $("focus-switch");
    switcher.replaceChildren();
    for (const quote of rankedQuotes()) {
      const item = el("li", null, quote.label || quote.symbol);
      if (quote.symbol === target.symbol) item.dataset.active = "1";
      item.dataset.symbol = quote.symbol;
      switcher.appendChild(item);
    }
  }

  function renderMinimal(payload) {
    const hero = payload.hero;
    if (!hero) return;
    const decimals = decimalsFor(hero.symbol, hero.price);
    const host = $("minimal-hero");
    host.replaceChildren();
    host.appendChild(el("span", "mh-label", `${hero.label} · ${hero.symbol}`));
    host.appendChild(el("span", "mh-price num", fmtPrice(hero.price, decimals)));
    const pct = el("span", "mh-pct num", `${arrowOf(trendOf(hero.changePct))} ${fmtPct(hero.changePct)}`);
    pct.style.color = trendOf(hero.changePct) === "up" ? "var(--up)" : trendOf(hero.changePct) === "down" ? "var(--down)" : "var(--flat)";
    host.appendChild(pct);

    const strip = $("minimal-strip");
    strip.replaceChildren();
    const ordered = rankedQuotes().filter((quote) => quote.symbol !== hero.symbol);
    for (const quote of ordered) {
      const item = el("span", null, shortLabel(quote));
      const value = el("b", "num", `${fmtPrice(quote.price, decimalsFor(quote.symbol, quote.price))} ${fmtPct(quote.changePct)}`);
      value.style.color = trendOf(quote.changePct) === "up" ? "var(--up)" : trendOf(quote.changePct) === "down" ? "var(--down)" : "var(--flat)";
      item.appendChild(value);
      strip.appendChild(item);
    }
  }

  function renderTape(payload) {
    const track = $("tape-track");
    const items = [];
    for (const quote of payload.quotes) {
      const node = el("span", "tape-item");
      node.appendChild(el("b", null, `${quote.label} ${fmtPrice(quote.price, decimalsFor(quote.symbol, quote.price))}`));
      const pct = el("i", null, fmtPct(quote.changePct));
      pct.style.color = trendOf(quote.changePct) === "up" ? "var(--up)" : trendOf(quote.changePct) === "down" ? "var(--down)" : "var(--flat)";
      node.appendChild(pct);
      items.push(node);
      if (payload.headlines.length) {
        const headline = payload.headlines[(items.length - 1) % payload.headlines.length];
        const news = el("span", "tape-item");
        news.appendChild(el("i", null, headline.source || "WIRE"));
        news.appendChild(el("b", null, headline.title));
        news.style.color = "var(--dim)";
        items.push(news);
      }
    }
    track.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const node of items) fragment.appendChild(node.cloneNode(true));
    for (const node of items) fragment.appendChild(node);
    track.appendChild(fragment);
    const duration = Math.max(40, items.length * 4.2);
    track.style.animation = state.static ? "none" : `tape-scroll ${duration}s linear infinite`;
  }

  function renderMeta(payload) {
    const stamp = new Date(payload.generatedAt);
    const sources = (payload.sources || []).map((source) => source.toUpperCase()).join(" · ") || "CACHE";
    $("tape-meta").textContent = `${T.updated} ${stamp.toLocaleTimeString("en-GB", { hour12: false })} · ${sources}`;
    $("tape-label").textContent = T.tape;
    const feed = $("feed-text");
    const errors = payload.errors || [];
    if (errors.length) {
      document.body.dataset.state = "stale";
      feed.textContent = `${T.stale} · ${errors.length}`;
      $("feed").title = errors.join("\n");
    } else {
      document.body.dataset.state = "live";
      feed.textContent = T.live;
      $("feed").title = "";
    }
  }

  function renderAll(payload) {
    state.data = payload;
    renderMeta(payload);
    renderSessions(payload.sessions);
    renderHero(payload.hero);
    renderRibbon(rankedQuotes());
    renderGauge(payload.sentiment);
    renderMovers(payload.quotes);
    if (state.mode === "board") renderBoard(payload);
    if (state.mode === "focus") renderFocus(payload);
    if (state.mode === "minimal") renderMinimal(payload);
    renderTape(payload);
    renderWidgets();
  }

  /* ---------------------------------------------------------------- fetch */

  async function load({ quiet = false } = {}) {
    if (!quiet) document.body.dataset.state = "loading";
    const query = new URLSearchParams();
    if (state.symbols) query.set("symbols", state.symbols);
    if (state.hero) query.set("hero", state.hero);
    try {
      const response = await fetch(`/api/wallpaper?${query.toString()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!payload || !payload.quotes) throw new Error("empty payload");
      renderAll(payload);
    } catch (error) {
      document.body.dataset.state = "error";
      $("feed-text").textContent = T.lost;
      $("feed").title = String(error);
    }
  }

  /* ---------------------------------------------------------------- modes */

  function applyMode(mode, { remember = true } = {}) {
    state.mode = MODES.includes(mode) ? mode : "horizon";
    document.body.dataset.mode = state.mode;
    if (remember) {
      const url = new URL(location.href);
      url.searchParams.set("mode", state.mode);
      history.replaceState(null, "", url);
    }
    if (state.data) {
      if (state.mode === "board") renderBoard(state.data);
      if (state.mode === "focus") renderFocus(state.data);
      if (state.mode === "minimal") renderMinimal(state.data);
      if (state.mode === "horizon") renderHero(state.data.hero);
    }
    startRotation();
  }

  function cycleHero() {
    if (!state.data) return;
    const quotes = rankedQuotes();
    const index = quotes.findIndex((quote) => quote.symbol === state.data.hero?.symbol);
    const next = quotes[(index + 1) % quotes.length];
    state.hero = next.symbol;
    state.focus = next.symbol;
    const url = new URL(location.href);
    url.searchParams.set("hero", next.symbol);
    history.replaceState(null, "", url);
    load({ quiet: true });
  }

  function promoteHero(symbol) {
    if (!state.data) return false;
    const quote = state.data.quotes.find((item) => item.symbol === symbol);
    if (!quote) return false;
    state.data.hero = quote;
    state.hero = symbol;
    state.focus = symbol;
    const url = new URL(location.href);
    url.searchParams.set("hero", symbol);
    history.replaceState(null, "", url);
    rerenderMode();
    return true;
  }

  function rerenderMode() {
    if (!state.data) return;
    if (state.mode === "horizon") renderHero(state.data.hero);
    if (state.mode === "board") renderBoard(state.data);
    if (state.mode === "focus") renderFocus(state.data);
    if (state.mode === "minimal") renderMinimal(state.data);
  }

  // Rotation follows the caller's priority list first, then everything else.
  function rotationSymbols() {
    const quotes = state.data ? rankedQuotes() : [];
    const seen = new Set();
    const ordered = [];
    const push = (symbol) => {
      if (!symbol || seen.has(symbol)) return;
      if (!quotes.some((quote) => quote.symbol === symbol)) return;
      seen.add(symbol);
      ordered.push(symbol);
    };
    state.rotateOrder.forEach(push);
    quotes.forEach((quote) => push(quote.symbol));
    return ordered;
  }

  function rotateDelay() {
    const override = state.rotateByMode[state.mode];
    if (override !== undefined) return override;
    if (state.mode === "board") return 0;
    return state.rotateMs;
  }

  function rotateNext() {
    if (!state.data || state.data.quotes.length < 2) return;
    const symbols = rotationSymbols();
    if (symbols.length < 2) return;
    const index = symbols.indexOf(state.data.hero?.symbol);
    promoteHero(symbols[(index + 1) % symbols.length]);
    renderAuto();
  }

  function renderAuto() {
    const node = $("hero-auto");
    if (!node) return;
    const delay = rotateDelay();
    const on = delay > 0 && !state.rotatePaused && !state.rotateLock;
    node.hidden = !(delay > 0 || state.rotateLock);
    if (!(delay > 0)) node.textContent = "";
    else if (state.rotateLock) node.textContent = "LOCKED";
    else if (state.rotatePaused) node.textContent = "PAUSED";
    else node.textContent = `AUTO · ${Math.round(delay / 1000)}S`;
    document.body.dataset.auto = on ? "1" : "0";
    document.body.dataset.locked = state.rotateLock ? "1" : "0";
  }

  function startRotation() {
    clearInterval(state.rotateTimer);
    renderAuto();
    const delay = rotateDelay();
    if (!delay || state.rotatePaused || state.rotateLock) return;
    state.rotateTimer = setInterval(rotateNext, delay);
  }

  function toggleRotation() {
    if (!rotateDelay() && !state.rotateLock) return;
    if (state.rotateLock) {
      state.rotateLock = false;
      state.rotatePaused = false;
    } else {
      state.rotatePaused = !state.rotatePaused;
    }
    startRotation();
  }

  // Clicking a tile or a switcher row pins that symbol and stops the rotation.
  function lockHero(symbol) {
    if (!promoteHero(symbol)) return;
    state.rotateLock = true;
    startRotation();
  }

  function showHint() {
    if (!state.hud) return;
    const hint = $("hint");
    hint.hidden = false;
    hint.dataset.show = "1";
    clearTimeout(state.hintTimer);
    state.hintTimer = setTimeout(() => {
      hint.dataset.show = "0";
      setTimeout(() => {
        hint.hidden = true;
      }, 240);
    }, 4200);
  }

  function bindKeys() {
    window.addEventListener("keydown", (event) => {
      const key = event.key.toLowerCase();
      if (key >= "1" && key <= "4") applyMode(MODES[Number(key) - 1]);
      else if (key === "h") {
        cycleHero();
        startRotation();
      } else if (key === "k") {
        const order = ["line", "candles", "tv"];
        state.chartStyle = order[(order.indexOf(state.chartStyle) + 1) % order.length];
        document.body.dataset.chart = state.chartStyle;
        if (state.data) {
          if (state.mode === "horizon") renderHero(state.data.hero);
          if (state.mode === "focus") renderFocus(state.data);
        }
        document.querySelectorAll("[data-widget-type='candles']").forEach((node) => {
          renderCandleWidget(node, { symbol: node.dataset.symbol });
        });
        persistLayout();
      } else if (key === "p") toggleRotation();
      else if (key === "r") load({ quiet: true });
      else if (key === "f") {
        if (document.fullscreenElement) document.exitFullscreen();
        else document.documentElement.requestFullscreen?.();
      } else if (key === "l") {
        state.edit = !state.edit;
        document.body.dataset.edit = state.edit ? "1" : "0";
        if (state.edit) {
          layout.beforeEdit = JSON.parse(JSON.stringify(layout.anchors));
          pinBlocks();
          showEditBanner();
        } else {
          layout.beforeEdit = null;
        }
        applyAnchors();
      } else if (key === "Escape" && state.edit) {
        exitEdit();
        return;
      } else if (key === "?") showHint();
      else return;
      showHint();
    });
    window.addEventListener("pointermove", (event) => {
      if (event.pointerType === "mouse") showHint();
    });
  }

  function startClock() {
    renderClock();
    clearInterval(state.clockTimer);
    state.clockTimer = setInterval(() => {
      renderClock();
      tickSessions();
      tickWidgetClocks();
    }, 1000);
  }

  function startRefresh() {
    clearInterval(state.timer);
    state.timer = setInterval(() => {
      if (document.hidden) return;
      load({ quiet: true });
    }, state.refreshMs);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearInterval(state.rotateTimer);
      } else {
        load({ quiet: true });
        startRotation();
      }
    });
  }

  function redraw() {
    if (!state.data) return;
    if (state.mode === "board") {
      document.querySelectorAll(".tile").forEach((tile, index) => {
        const quote = state.data.quotes[index];
        const spark = tile.querySelector(".tile-spark");
        if (quote && spark) drawSpark(spark, quote.series || []);
      });
    }
    if (state.mode === "focus") {
      const target = state.data.quotes.find((quote) => quote.symbol === state.focus) || state.data.hero;
      if (target) {
        paintPriceChart(
          $("focus-chart"),
          target,
          {
            ghost: target.seriesSource === "flat",
            baseline: target.previousClose,
            padLeft: 8,
            padRight: 132,
            padTop: 18,
            padBottom: 34,
            timeZone: target.seriesTimezone || "America/New_York",
          },
          {
            padLeft: 54,
            padRight: 132,
            padTop: 18,
            padBottom: 34,
            timeZone: target.seriesTimezone || "America/New_York",
            bars: 150,
          },
        );
      }
    }
    if (state.mode === "horizon" && state.data.hero) {
      paintPriceChart(
        $("horizon-chart"),
        state.data.hero,
        {
          ghost: state.data.hero.seriesSource === "flat",
          baseline: state.data.hero.previousClose,
          padRight: 118,
          padBottom: 44,
          timeZone: state.data.hero.seriesTimezone || "America/New_York",
        },
        {
          padLeft: 54,
          padRight: 118,
          padBottom: 40,
          timeZone: state.data.hero.seriesTimezone || "America/New_York",
          bars: 120,
        },
      );
    }
  }

  function runDebugWidgetOps() {
    const add = params.get("addwidget");
    if (add) {
      const [type, symbol] = add.split(":");
      addWidget(type || "candles", symbol || "");
    }
    const remove = params.get("removewidget");
    if (remove) removeWidget(remove);
  }

  function init() {
    applyLayout();
    pruneWidgetAnchors();
    applyCopy();
    applyBlocks();
    applyAnchors();
    state.edit = layout.edit;
    document.body.dataset.edit = state.edit ? "1" : "0";
    if (state.edit) {
      layout.beforeEdit = JSON.parse(JSON.stringify(layout.anchors));
      pinBlocks();
      applyAnchors();
      renderWidgets();
      showEditBanner();
    }
    document.querySelectorAll("[data-block]").forEach(makeDraggable);
    window.DeskTapeLayout = exportLayout;
  window.DeskTapePost = persistLayout;

  // Probe hook: ?probe=1 reports raw pointer events to the host so an OS-level
  // click can be proven to arrive inside the web view.
  if (DEBUG && params.get("probe") === "1") {
    const report = (kind) => (event) => {
      const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.desktape;
      if (!bridge) return;
      try {
        bridge.postMessage({ probe: kind, x: Math.round(event.clientX), y: Math.round(event.clientY) });
      } catch (error) {
        /* ignore */
      }
    };
    document.addEventListener("pointerdown", report("down"), true);
    document.addEventListener("pointerup", report("up"), true);
  }
    document.body.dataset.static = state.static ? "1" : "0";
    document.body.dataset.state = "loading";
    $("hint").hidden = !state.hud;
    if (!state.hero && state.data) state.hero = state.data.hero?.symbol || "";
    applyMode(state.mode, { remember: false });
    startClock();
    bindKeys();
    if (DEBUG) runDebugWidgetOps();
    load();
    startRefresh();
    if (DEBUG && params.get("autodrag") === "1" && state.edit) {
      // Debug aid: replay a pointer drag so the drag → anchor → host path can be
      // verified without a human at the mouse.
      setTimeout(() => {
        const node = document.querySelector('[data-block="hero"]');
        if (!node) return;
        const box = node.getBoundingClientRect();
        const x = box.left + box.width / 2;
        const y = box.top + box.height / 2;
        const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };
        node.dispatchEvent(new PointerEvent("pointerdown", opts));
        window.dispatchEvent(new PointerEvent("pointermove", { ...opts, clientX: x + 160, clientY: y + 90 }));
        window.dispatchEvent(new PointerEvent("pointerup", { ...opts, clientX: x + 160, clientY: y + 90 }));
        setTimeout(persistLayout, 400);
      }, 1200);
    }
    if (params.get("autosave") === "1" && state.edit) {
      // Debug aid: prove the page → host layout channel without a human drag.
      setTimeout(persistLayout, 2500);
    }
    startRotation();
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) clearInterval(state.rotateTimer);
      else startRotation();
    });
    let resizeFrame = null;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(redraw);
    });
    if (params.get("debug") === "1") {
      setTimeout(() => {
        const rect = (node) => {
          if (!node) return null;
          const box = node.getBoundingClientRect();
          return [Math.round(box.top), Math.round(box.height)];
        };
        document.title = JSON.stringify({
          viewport: [innerWidth, innerHeight],
          stage: rect(document.querySelector(".stage")),
          mode: rect(document.querySelector(".mode-horizon")),
          chart: rect($("horizon-chart")),
          tape: rect(document.querySelector(".tape")),
          scrollHeight: document.documentElement.scrollHeight,
          layout: exportLayout(),
        });
      }, 2500);
    }
    document.addEventListener("click", (event) => {
      const pinned = event.target.closest?.(".focus-switch li, .tile");
      if (!pinned) return;
      const symbol = pinned.dataset.symbol;
      if (!symbol) return;
      if (state.rotateLock && state.data?.hero?.symbol === symbol) {
        state.rotateLock = false;
        startRotation();
        return;
      }
      lockHero(symbol);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
