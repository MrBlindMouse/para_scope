/** Shared Chart.js helpers for dashboard widgets. */
(function (global) {
  "use strict";

  var charts = Object.create(null);
  var PALETTE = [
    "#3366cc", "#dc3912", "#ff9900", "#109618", "#990099",
    "#0099c6", "#dd4477", "#66aa00", "#b82e2e", "#316395",
  ];

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function destroy(canvasId) {
    if (charts[canvasId]) {
      charts[canvasId].destroy();
      delete charts[canvasId];
    }
  }

  function colors(n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(PALETTE[i % PALETTE.length]);
    return out;
  }

  /** Accept flat [{ts,v}] or multi [{name, points:[{ts,v}]}]. */
  function normalizeSeries(series) {
    if (!series || !series.length) return [];
    if (series[0] && Array.isArray(series[0].points)) return series;
    return [{ name: "", points: series }];
  }

  function unionLabels(seriesList) {
    var seen = Object.create(null);
    var labels = [];
    seriesList.forEach(function (s) {
      (s.points || []).forEach(function (d) {
        var key = d.ts;
        if (seen[key]) return;
        seen[key] = true;
        labels.push(key);
      });
    });
    labels.sort();
    return labels;
  }

  function formatTs(ts) {
    try {
      var dt = new Date(ts);
      return isNaN(dt.getTime()) ? ts : dt.toLocaleString();
    } catch (e) {
      return ts;
    }
  }

  function renderLine(canvas, series, opts) {
    if (!canvas || typeof Chart === "undefined") return null;
    opts = opts || {};
    var id = canvas.id;
    destroy(id);
    var seriesList = normalizeSeries(series);
    var rawLabels = unionLabels(seriesList);
    var labels = rawLabels.map(formatTs);
    var style = opts.style || "default";
    var tension = style === "smooth" ? 0.4 : (style === "stepped" ? 0 : 0.2);
    var stepped = style === "stepped";
    var showMarkers = style === "markers" || (!opts.spark && style !== "default" && style !== "smooth" && style !== "stepped" && style !== "filled");
    if (style === "markers") showMarkers = true;
    if (opts.spark && style !== "markers") showMarkers = false;
    var fill = !!opts.area || style === "filled";
    var link = cssVar("--color-link", "#3366cc");
    var muted = cssVar("--color-ink-muted", "#888");
    var border = cssVar("--color-border", "#ccc");
    var multi = seriesList.length > 1;
    var datasets = seriesList.map(function (s, i) {
      var byTs = Object.create(null);
      (s.points || []).forEach(function (d) { byTs[d.ts] = d.v; });
      var color = multi ? PALETTE[i % PALETTE.length] : link;
      return {
        label: s.name || opts.label || "",
        data: rawLabels.map(function (ts) {
          return byTs[ts] != null ? byTs[ts] : null;
        }),
        borderColor: color,
        backgroundColor: fill ? color + "33" : "transparent",
        fill: fill,
        tension: tension,
        stepped: stepped,
        pointRadius: showMarkers ? (opts.spark ? 1 : 3) : (opts.spark ? 0 : 2),
        borderWidth: opts.spark ? 1.5 : 2,
        spanGaps: true,
      };
    });
    var chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            display: multi || (!!opts.label && !opts.spark),
            position: "bottom",
          },
          tooltip: { enabled: !opts.spark },
        },
        scales: {
          x: {
            display: !opts.spark,
            ticks: { color: muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
            grid: { color: border },
          },
          y: {
            display: !opts.spark,
            ticks: {
              color: muted,
              callback: function (v) {
                return opts.unit ? v + " " + opts.unit : v;
              },
            },
            grid: { color: border },
          },
        },
      },
    });
    charts[id] = chart;
    return chart;
  }

  function legendOpts(style) {
    if (style === "no_legend") return { display: false };
    if (style === "legend_right") return { display: true, position: "right" };
    return { display: true, position: "bottom" };
  }

  function renderPie(canvas, labels, values, opts) {
    if (!canvas || typeof Chart === "undefined") return null;
    opts = opts || {};
    destroy(canvas.id);
    var doughnut = opts.display === "doughnut";
    var chart = new Chart(canvas.getContext("2d"), {
      type: doughnut ? "doughnut" : "pie",
      data: {
        labels: labels || [],
        datasets: [{
          data: values || [],
          backgroundColor: colors((values || []).length),
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: legendOpts(opts.style) },
      },
    });
    charts[canvas.id] = chart;
    return chart;
  }

  function renderBar(canvas, labels, values, opts) {
    if (!canvas || typeof Chart === "undefined") return null;
    opts = opts || {};
    destroy(canvas.id);
    var stacked = opts.display === "stacked_bar";
    var horizontal = opts.style === "horizontal";
    var muted = cssVar("--color-ink-muted", "#888");
    var border = cssVar("--color-border", "#ccc");
    var datasets;
    if (stacked) {
      datasets = (labels || []).map(function (lab, i) {
        return {
          label: lab,
          data: [values[i]],
          backgroundColor: PALETTE[i % PALETTE.length],
        };
      });
    } else {
      datasets = [{
        label: opts.label || "",
        data: values || [],
        backgroundColor: colors((values || []).length),
      }];
    }
    var indexAxis = horizontal ? "y" : "x";
    var chart = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: stacked ? ["Total"] : (labels || []),
        datasets: datasets,
      },
      options: {
        indexAxis: indexAxis,
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: legendOpts(opts.style === "no_legend" ? "no_legend" : (stacked ? "default" : (opts.style || "default"))),
        },
        scales: {
          x: {
            stacked: stacked,
            ticks: { color: muted },
            grid: { color: border },
          },
          y: {
            stacked: stacked,
            ticks: {
              color: muted,
              callback: function (v) {
                return opts.unit ? v + " " + opts.unit : v;
              },
            },
            grid: { color: border },
          },
        },
      },
    });
    charts[canvas.id] = chart;
    return chart;
  }

  function initIn(root) {
    root = root || document;
    root.querySelectorAll("canvas[data-widget-chart]").forEach(function (canvas) {
      var kind = canvas.getAttribute("data-widget-chart");
      var display = canvas.getAttribute("data-chart-display") || "";
      var style = canvas.getAttribute("data-chart-style") || "default";
      if (kind === "aggregate") {
        var labels = [];
        var values = [];
        try { labels = JSON.parse(canvas.getAttribute("data-labels") || "[]"); } catch (e) {}
        try { values = JSON.parse(canvas.getAttribute("data-values") || "[]"); } catch (e) {}
        var unit = canvas.getAttribute("data-unit") || "";
        if (display === "bar" || display === "stacked_bar") {
          renderBar(canvas, labels, values, { display: display, unit: unit, style: style });
        } else {
          renderPie(canvas, labels, values, { display: display || "pie", style: style });
        }
        return;
      }
      var series = [];
      try {
        series = JSON.parse(canvas.getAttribute("data-series") || "[]");
      } catch (e) {
        series = [];
      }
      renderLine(canvas, series, {
        label: canvas.getAttribute("data-label") || "",
        unit: canvas.getAttribute("data-unit") || "",
        spark: canvas.getAttribute("data-spark") === "1" || display === "sparkline",
        area: canvas.getAttribute("data-area") === "1" || display === "area",
        style: style,
      });
    });
  }

  global.ParaScopeCharts = {
    renderLine: renderLine,
    renderPie: renderPie,
    renderBar: renderBar,
    destroy: destroy,
    initIn: initIn,
  };

  document.addEventListener("DOMContentLoaded", function () {
    initIn(document);
  });

  document.body.addEventListener("htmx:afterSwap", function (e) {
    initIn(e.target);
  });
})(window);
