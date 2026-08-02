/** Shared ApexCharts helpers for dashboard widgets (display + style). */
(function (global) {
  "use strict";

  var charts = Object.create(null);
  var chartObservers = Object.create(null);
  var PALETTE = [
    "#3366cc", "#dc3912", "#ff9900", "#109618", "#990099",
    "#0099c6", "#dd4477", "#66aa00", "#b82e2e", "#316395",
  ];

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function destroy(id) {
    if (chartObservers[id]) {
      try { chartObservers[id].disconnect(); } catch (e) {}
      delete chartObservers[id];
    }
    if (charts[id]) {
      try { charts[id].destroy(); } catch (e) {}
      delete charts[id];
    }
  }

  function colors(n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(PALETTE[i % PALETTE.length]);
    return out;
  }

  function theme() {
    return {
      link: cssVar("--color-link", "#3366cc"),
      muted: cssVar("--color-ink-muted", "#888"),
      border: cssVar("--color-border", "#ccc"),
      ink: cssVar("--color-ink", "#222"),
      surface: cssVar("--color-surface", "#fff"),
    };
  }

  function normalizeSeries(series) {
    if (!series || !series.length) return [];
    if (series[0] && Array.isArray(series[0].points)) return series;
    return [{ name: "", points: series }];
  }

  function pointX(ts) {
    if (typeof ts === "number" && isFinite(ts)) return ts;
    var ms = Date.parse(ts);
    return isFinite(ms) ? ms : NaN;
  }

  /** Pack series into Apex datetime [{x,y}] points (no category null-padding). */
  function seriesPayload(series) {
    var seriesList = normalizeSeries(series);
    var apexSeries = seriesList.map(function (s, i) {
      var byX = Object.create(null);
      (s.points || []).forEach(function (d) {
        var x = pointX(d.ts);
        var y = Number(d.v);
        if (!isFinite(x) || !isFinite(y)) return;
        byX[x] = y;  // last wins on duplicate instant
      });
      var xs = Object.keys(byX).map(Number).sort(function (a, b) { return a - b; });
      return {
        name: s.name || ("Series " + (i + 1)),
        data: xs.map(function (x) { return { x: x, y: byX[x] }; }),
      };
    });
    return { seriesList: seriesList, apexSeries: apexSeries };
  }

  function formatTs(ts) {
    try {
      var dt = new Date(ts);
      if (isNaN(dt.getTime())) return String(ts);
      var tz = "UTC";
      if (typeof document !== "undefined" && document.documentElement) {
        tz = document.documentElement.getAttribute("data-display-timezone") || "UTC";
      }
      return new Intl.DateTimeFormat(undefined, {
        timeZone: tz,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(dt);
    } catch (e) {
      return String(ts);
    }
  }

  function tooltipTheme() {
    try {
      if (typeof window !== "undefined" && window.matchMedia &&
          window.matchMedia("(prefers-color-scheme: dark)").matches) {
        return "dark";
      }
    } catch (e) {}
    return "light";
  }

  function baseChartOpts(type, extra) {
    var t = theme();
    var opts = {
      chart: {
        type: type,
        height: "100%",
        width: "100%",
        fontFamily: "inherit",
        background: "transparent",
        toolbar: { show: false },
        animations: { enabled: false },
        zoom: { enabled: false },
        parentHeightOffset: 0,
      },
      colors: PALETTE.slice(),
      grid: {
        borderColor: t.border,
        strokeDashArray: 0,
        padding: { left: 4, right: 4, top: 0, bottom: 0 },
      },
      legend: {
        labels: { colors: t.muted },
        fontSize: "12px",
      },
      tooltip: { theme: tooltipTheme() },
      dataLabels: { enabled: false },
      stroke: { width: 2, curve: "smooth" },
    };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        if (extra[k] == null) return;  // keep defaults (e.g. grid)
        if (k === "chart" && extra.chart) {
          Object.assign(opts.chart, extra.chart);
        } else if (
          extra[k] && typeof extra[k] === "object" && !Array.isArray(extra[k]) &&
          opts[k] && typeof opts[k] === "object" && !Array.isArray(opts[k])
        ) {
          opts[k] = Object.assign({}, opts[k], extra[k]);
        } else {
          opts[k] = extra[k];
        }
      });
    }
    return opts;
  }

  function mount(el, options) {
    if (!el || typeof ApexCharts === "undefined") return null;
    var id = el.id || el.getAttribute("id");
    if (!id) {
      id = "apex-" + Math.random().toString(36).slice(2, 10);
      el.id = id;
    }
    destroy(id);
    el.innerHTML = "";
    var chart = new ApexCharts(el, options);
    chart.render();
    charts[id] = chart;
    if (typeof ResizeObserver !== "undefined") {
      var timer = null;
      var ro = new ResizeObserver(function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          try { chart.resize(); } catch (e) {}
        }, 50);
      });
      var observeEl = (el.closest && el.closest(".widget-chart")) || el;
      ro.observe(observeEl);
      chartObservers[id] = ro;
    }
    return chart;
  }

  function legendVisible(opts) {
    return !!(opts && opts.showLegend !== false && !opts.preview);
  }

  function parseNum(el, attr, fallback) {
    var n = parseFloat(el.getAttribute(attr));
    return isFinite(n) ? n : fallback;
  }

  function parseMax(el) {
    var n = parseNum(el, "data-max", 100);
    return n > 0 ? n : 100;
  }

  function pctOfMax(value, max) {
    if (!max || max <= 0) return 0;
    var p = (Number(value) / max) * 100;
    if (!isFinite(p)) return 0;
    return Math.max(0, Math.min(100, p));
  }

  function unitFmt(unit) {
    return function (v) {
      return unit ? v + " " + unit : String(v);
    };
  }

  function axisLabels(opts, t) {
    if (opts.preview) {
      return { show: false };
    }
    return {
      style: { colors: t.muted },
      rotate: 0,
      hideOverlappingLabels: true,
      formatter: function (val) { return formatTs(val); },
    };
  }

  function timeAxis(opts, t) {
    return {
      type: "datetime",
      labels: axisLabels(opts, t),
      axisBorder: { color: t.border },
      axisTicks: { color: t.border },
    };
  }

  function renderLine(el, series, opts) {
    opts = opts || {};
    var t = theme();
    var style = opts.style || "basic";
    var packed = seriesPayload(series);
    var multi = packed.seriesList.length > 1 || style === "multi";
    var showLabels = style === "labels";
    var curve = style === "stepline" ? "stepline" : "straight";
    return mount(el, baseChartOpts("line", {
      colors: multi ? PALETTE.slice() : [t.link],
      series: packed.apexSeries,
      stroke: { width: 2, curve: curve },
      markers: { size: showLabels ? 3 : 0 },
      dataLabels: {
        enabled: showLabels,
        style: { colors: [t.ink], fontSize: opts.preview ? "9px" : "10px" },
      },
      legend: {
        show: legendVisible(opts),
        position: "bottom",
        labels: { colors: t.muted },
      },
      xaxis: timeAxis(opts, t),
      yaxis: {
        labels: {
          show: !opts.preview,
          style: { colors: t.muted },
          formatter: unitFmt(opts.unit),
        },
      },
      grid: opts.preview ? {
        borderColor: t.border,
        padding: { left: 4, right: 4, top: 0, bottom: 0 },
      } : undefined,
    }));
  }

  function renderArea(el, series, opts) {
    opts = opts || {};
    var t = theme();
    var style = opts.style || "basic";
    var packed = seriesPayload(series);
    var stacked = style === "stacked";
    var negative = style === "negative";
    return mount(el, baseChartOpts("area", {
      chart: { stacked: stacked, type: "area" },
      colors: packed.seriesList.length > 1 ? PALETTE.slice() : [t.link],
      series: packed.apexSeries,
      stroke: { width: 2, curve: "straight" },
      fill: {
        type: "solid",
        opacity: negative ? 0.35 : 0.25,
      },
      legend: {
        show: legendVisible(opts),
        position: "bottom",
        labels: { colors: t.muted },
      },
      xaxis: timeAxis(opts, t),
      yaxis: {
        labels: {
          show: !opts.preview,
          style: { colors: t.muted },
          formatter: unitFmt(opts.unit),
        },
      },
      grid: opts.preview ? {
        borderColor: t.border,
        padding: { left: 4, right: 4, top: 0, bottom: 0 },
      } : undefined,
    }));
  }

  function renderColumn(el, series, opts) {
    opts = opts || {};
    var t = theme();
    var style = opts.style || "basic";
    var packed = seriesPayload(series);
    var stacked = style === "stacked" || style === "stacked_100";
    var showLabels = style === "labels";
    var horizontal = !!opts.horizontal;
    var extra = {
      chart: {
        type: "bar",
        stacked: stacked,
      },
      series: packed.apexSeries,
      colors: packed.seriesList.length > 1 ? PALETTE.slice() : [t.link],
      plotOptions: {
        bar: {
          horizontal: horizontal,
          borderRadius: 2,
          columnWidth: opts.preview ? "70%" : "55%",
          dataLabels: { position: horizontal ? "center" : "top" },
        },
      },
      dataLabels: {
        enabled: showLabels,
        style: { colors: [t.ink], fontSize: opts.preview ? "9px" : "10px" },
        offsetY: opts.preview && !horizontal ? -4 : 0,
      },
      legend: {
        show: legendVisible(opts),
        position: "bottom",
        labels: { colors: t.muted },
      },
      xaxis: timeAxis(opts, t),
      yaxis: {
        labels: {
          show: !opts.preview,
          style: { colors: t.muted },
          formatter: unitFmt(opts.unit),
        },
      },
      grid: opts.preview ? {
        borderColor: t.border,
        padding: { left: 2, right: 2, top: showLabels ? 8 : 0, bottom: 0 },
      } : undefined,
    };
    if (style === "stacked_100") {
      extra.chart.stackType = "100%";
    }
    return mount(el, baseChartOpts("bar", extra));
  }

  function renderPie(el, labels, values, opts) {
    opts = opts || {};
    var t = theme();
    var donut = opts.style === "donut";
    var pieOpts = {
      series: (values || []).map(Number),
      labels: labels || [],
      colors: colors((values || []).length),
      legend: {
        show: legendVisible(opts),
        position: "bottom",
        labels: { colors: t.muted },
      },
      stroke: { width: 1, colors: [t.surface] },
      dataLabels: {
        enabled: true,
        style: { fontSize: opts.preview ? "10px" : "12px" },
      },
      grid: opts.preview ? { padding: { left: 0, right: 0, top: 0, bottom: 0 } } : undefined,
    };
    if (donut) {
      pieOpts.plotOptions = {
        pie: {
          expandOnClick: false,
          donut: {
            size: opts.preview ? "45%" : "55%",
            labels: {
              show: !opts.preview,
              name: { color: t.muted },
              value: { color: t.ink },
              total: { show: true, label: "Total", color: t.muted },
            },
          },
        },
      };
    }
    return mount(el, baseChartOpts(donut ? "donut" : "pie", pieOpts));
  }

  function renderRadar(el, labels, values, opts) {
    opts = opts || {};
    var t = theme();
    return mount(el, baseChartOpts("radar", {
      series: [{ name: opts.unit || "Value", data: (values || []).map(Number) }],
      labels: labels || [],
      colors: [t.link],
      markers: { size: opts.preview ? 2 : 3 },
      stroke: { width: 2 },
      fill: { opacity: 0.25 },
      yaxis: { show: false },
      legend: { show: legendVisible(opts) },
      xaxis: {
        labels: {
          style: {
            colors: t.muted,
            fontSize: opts.preview ? "10px" : "12px",
          },
        },
      },
      plotOptions: {
        radar: {
          polygons: {
            strokeColors: t.border,
            connectorColors: t.border,
          },
        },
      },
      grid: opts.preview
        ? { padding: { left: 12, right: 12, top: 8, bottom: 8 } }
        : undefined,
    }));
  }

  function renderPolar(el, labels, values, opts) {
    opts = opts || {};
    var t = theme();
    return mount(el, baseChartOpts("polarArea", {
      series: (values || []).map(Number),
      labels: labels || [],
      colors: colors((values || []).length),
      stroke: { colors: [t.surface] },
      fill: { opacity: 0.85 },
      legend: {
        show: legendVisible(opts),
        position: "bottom",
        labels: { colors: t.muted },
      },
      yaxis: { show: false },
      grid: opts.preview
        ? { padding: { left: 4, right: 4, top: 4, bottom: 4 } }
        : undefined,
    }));
  }

  function renderRadial(el, labels, values, opts) {
    opts = opts || {};
    var t = theme();
    var style = opts.style || "basic";
    var max = opts.max > 0 ? opts.max : 100;
    var unit = opts.unit || "";
    var labs = labels || [];
    var vals = values || [];
    var multi = style === "multi_band" || style === "custom_angle";
    var seriesVals = multi
      ? vals.map(function (v) { return pctOfMax(v, max); })
      : [pctOfMax(vals[0] || 0, max)];
    var seriesLabels = multi ? labs : [labs[0] || "Value"];

    if (style === "needle" || style === "gauge_ticks" || style === "stroked_gauge") {
      var value = Number(vals[0] || 0);
      var radialBar = {
        startAngle: -135,
        endAngle: 135,
        min: 0,
        max: max,
        hollow: { size: style === "stroked_gauge" ? "70%" : "55%" },
        track: {
          background: t.border,
          strokeWidth: style === "stroked_gauge" ? "70%" : "100%",
        },
        dataLabels: {
          name: {
            show: !!seriesLabels[0],
            color: t.muted,
            fontSize: "12px",
            offsetY: 24,
          },
          value: {
            show: true,
            color: t.ink,
            fontSize: "22px",
            fontWeight: 600,
            offsetY: -8,
            formatter: function () {
              return unit ? value + " " + unit : String(value);
            },
          },
        },
      };
      if (style === "needle") {
        radialBar.shape = "needle";
        radialBar.needle = {
          color: t.ink,
          length: "85%",
          baseWidth: 8,
          tipWidth: 1,
          showValueArc: true,
        };
      }
      if (style === "gauge_ticks") {
        radialBar.ticks = {
          show: true,
          major: { count: 11, length: 10, width: 2, color: t.muted, placement: "outside" },
          minor: { count: 3, length: 5, width: 1, color: t.border, placement: "outside" },
          labels: {
            show: true,
            offset: 6,
            fontSize: "10px",
            color: t.muted,
            formatter: function (v) { return Math.round(v); },
          },
        };
      }
      return mount(el, baseChartOpts(style === "needle" ? "gauge" : "radialBar", {
        series: style === "needle" ? [value] : [pctOfMax(value, max)],
        labels: seriesLabels,
        colors: [t.link],
        plotOptions: { radialBar: radialBar },
        stroke: style === "stroked_gauge" ? { lineCap: "round", dashArray: 4 } : { lineCap: "round" },
        legend: { show: false },
      }));
    }

    var startAngle = opts.startAngle != null ? opts.startAngle : -90;
    var endAngle = opts.endAngle != null ? opts.endAngle : 90;
    if (style === "basic" || style === "gradient") {
      startAngle = -90;
      endAngle = 90;
    } else if (style === "multi_band") {
      startAngle = 0;
      endAngle = 360;
    }

    var fill = style === "gradient"
      ? { type: "gradient", gradient: { shade: "light", type: "horizontal", opacityFrom: 1, opacityTo: 0.6 } }
      : { type: "solid" };

    return mount(el, baseChartOpts("radialBar", {
      series: seriesVals,
      labels: seriesLabels,
      colors: colors(seriesVals.length),
      fill: fill,
      plotOptions: {
        radialBar: {
          startAngle: startAngle,
          endAngle: endAngle,
          hollow: { size: seriesVals.length > 1 ? "30%" : "50%" },
          track: { background: t.border },
          dataLabels: {
            name: { color: t.muted, fontSize: "12px" },
            value: {
              color: t.ink,
              fontSize: "16px",
              formatter: function (v) { return Math.round(v) + "%"; },
            },
            total: {
              show: true,
              label: "Max",
              color: t.muted,
              formatter: function () {
                return unit ? max + " " + unit : String(max);
              },
            },
          },
        },
      },
      legend: {
        show: legendVisible(opts),
        position: "bottom",
        labels: { colors: t.muted },
      },
      stroke: { lineCap: "round" },
    }));
  }

  function initIn(root) {
    root = root || document;
    root.querySelectorAll("[data-widget-chart]").forEach(function (el) {
      var kind = el.getAttribute("data-widget-chart");
      var display = el.getAttribute("data-chart-display") || "";
      var style = el.getAttribute("data-chart-style") || "";
      var unit = el.getAttribute("data-unit") || "";
      var showLegend = el.getAttribute("data-show-legend") !== "0";

      if (kind === "series") {
        var series = [];
        try { series = JSON.parse(el.getAttribute("data-series") || "[]"); } catch (e) { series = []; }
        var sopts = {
          style: style || "basic",
          unit: unit,
          horizontal: el.getAttribute("data-horizontal") === "1",
          showLegend: showLegend,
        };
        if (display === "area") renderArea(el, series, sopts);
        else if (display === "column") renderColumn(el, series, sopts);
        else renderLine(el, series, sopts);
        return;
      }

      if (kind === "aggregate") {
        var labels = [];
        var values = [];
        try { labels = JSON.parse(el.getAttribute("data-labels") || "[]"); } catch (e) {}
        try { values = JSON.parse(el.getAttribute("data-values") || "[]"); } catch (e) {}
        var copts = {
          style: style,
          unit: unit,
          max: parseMax(el),
          startAngle: parseNum(el, "data-start-angle", -90),
          endAngle: parseNum(el, "data-end-angle", 90),
          showLegend: showLegend,
        };
        if (display === "radial") renderRadial(el, labels, values, copts);
        else if (display === "radar") renderRadar(el, labels, values, copts);
        else if (display === "polar") renderPolar(el, labels, values, copts);
        else renderPie(el, labels, values, copts);
      }
    });
  }

  function previewPoints(vals) {
    // Hourly UTC samples so datetime axis previews work.
    var start = Date.UTC(2026, 0, 1, 12, 0, 0);
    return vals.map(function (v, i) {
      return { ts: start + i * 3600000, v: v };
    });
  }

  /** Fixed sample payloads per display+style for config UI previews. */
  function previewSample(display, style) {
    var wave = [12, 18, 14, 22, 16, 28, 20, 24];
    var waveB = [8, 11, 9, 15, 12, 18, 14, 16];
    var negWave = [6, -4, 8, -10, 3, 12, -2, 5];
    // Strongly varied shares so stacked_100 columns don't look alike.
    var shareA = [90, 15, 50, 5, 75];
    var shareB = [10, 85, 50, 95, 25];
    var dual = [
      { name: "Alpha", points: previewPoints(wave) },
      { name: "Beta", points: previewPoints(waveB) },
    ];
    var dualShares = [
      { name: "Alpha", points: previewPoints(shareA) },
      { name: "Beta", points: previewPoints(shareB) },
    ];
    var single = [{ name: "Series", points: previewPoints(wave) }];
    var negSeries = [{ name: "Delta", points: previewPoints(negWave) }];

    if (display === "line") {
      if (style === "multi") return { kind: "series", series: dual };
      return { kind: "series", series: single };
    }
    if (display === "area") {
      if (style === "stacked") return { kind: "series", series: dual };
      if (style === "negative") return { kind: "series", series: negSeries };
      return { kind: "series", series: single };
    }
    if (display === "column") {
      if (style === "stacked_100") return { kind: "series", series: dualShares };
      if (style === "stacked") return { kind: "series", series: dual };
      if (style === "negative") return { kind: "series", series: negSeries };
      return { kind: "series", series: single };
    }
    if (display === "pie") {
      return {
        kind: "aggregate",
        labels: ["A", "B", "C"],
        values: [44, 28, 18],
      };
    }
    if (display === "radar" || display === "polar") {
      return {
        kind: "aggregate",
        labels: ["A", "B", "C", "D", "E"],
        values: [80, 55, 70, 40, 65],
      };
    }
    // radial
    if (style === "multi_band" || style === "custom_angle") {
      return {
        kind: "aggregate",
        labels: ["Used", "Reserved", "Free"],
        values: [67, 45, 28],
        max: 100,
        startAngle: style === "custom_angle" ? -90 : 0,
        endAngle: style === "custom_angle" ? 90 : 360,
      };
    }
    return {
      kind: "aggregate",
      labels: ["Load"],
      values: [72],
      max: 100,
    };
  }

  function renderPreview(el, display, style) {
    if (!el) return null;
    var sample = previewSample(display, style);
    var opts = {
      style: style || "basic",
      unit: "",
      preview: true,
      horizontal: el.getAttribute("data-horizontal") === "1",
    };
    if (sample.kind === "series") {
      if (display === "area") return renderArea(el, sample.series, opts);
      if (display === "column") return renderColumn(el, sample.series, opts);
      return renderLine(el, sample.series, opts);
    }
    opts.max = sample.max != null ? sample.max : 100;
    var attrStart = el.getAttribute("data-start-angle");
    var attrEnd = el.getAttribute("data-end-angle");
    if (attrStart != null && attrStart !== "") opts.startAngle = Number(attrStart);
    else if (sample.startAngle != null) opts.startAngle = sample.startAngle;
    if (attrEnd != null && attrEnd !== "") opts.endAngle = Number(attrEnd);
    else if (sample.endAngle != null) opts.endAngle = sample.endAngle;
    if (display === "radial") {
      return renderRadial(el, sample.labels, sample.values, opts);
    }
    if (display === "radar") {
      return renderRadar(el, sample.labels, sample.values, opts);
    }
    if (display === "polar") {
      return renderPolar(el, sample.labels, sample.values, opts);
    }
    return renderPie(el, sample.labels, sample.values, opts);
  }

  function destroyIn(root) {
    root = root || document;
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("[data-widget-chart], [data-preview-apex]").forEach(function (el) {
      var id = el.id || el.getAttribute("id");
      if (id) destroy(id);
    });
  }

  function initPreviews(root) {
    root = root || document;
    destroyIn(root);
    root.querySelectorAll("[data-preview-apex]").forEach(function (el) {
      renderPreview(
        el,
        el.getAttribute("data-chart-display") || "",
        el.getAttribute("data-chart-style") || ""
      );
    });
  }

  global.ParaScopeCharts = {
    renderLine: renderLine,
    renderArea: renderArea,
    renderColumn: renderColumn,
    renderPie: renderPie,
    renderRadial: renderRadial,
    renderRadar: renderRadar,
    renderPolar: renderPolar,
    renderPreview: renderPreview,
    initPreviews: initPreviews,
    destroy: destroy,
    destroyIn: destroyIn,
    packSeries: seriesPayload,
    initIn: initIn,
  };

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("DOMContentLoaded", function () {
      initIn(document);
    });
    if (document.body) {
      document.body.addEventListener("htmx:afterSwap", function (e) {
        initIn(e.target);
      });
    } else {
      document.addEventListener("DOMContentLoaded", function () {
        document.body.addEventListener("htmx:afterSwap", function (e) {
          initIn(e.target);
        });
      });
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { packSeries: seriesPayload, pointX: pointX };
  }
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);

