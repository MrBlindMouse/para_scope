#!/usr/bin/env node
/** Assert seriesPayload does not null-pad staggered multi-series. */
"use strict";

var pack = require("./widget-charts.js").packSeries;

function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    process.exit(1);
  }
}

var staggered = [
  {
    name: "A",
    points: [
      { ts: "2026-01-01T00:00:00+00:00", v: 1 },
      { ts: "2026-01-01T00:02:00+00:00", v: 2 },
    ],
  },
  {
    name: "B",
    points: [
      { ts: "2026-01-01T00:01:00Z", v: 10 },
      { ts: "2026-01-01T00:03:00Z", v: 20 },
    ],
  },
];

var packed = pack(staggered);
assert(packed.apexSeries.length === 2, "two series");
assert(packed.apexSeries[0].data.length === 2, "A keeps 2 points (no null pad)");
assert(packed.apexSeries[1].data.length === 2, "B keeps 2 points (no null pad)");
packed.apexSeries.forEach(function (s) {
  s.data.forEach(function (pt) {
    assert(pt && typeof pt.x === "number" && isFinite(pt.x), "x is ms");
    assert(typeof pt.y === "number" && isFinite(pt.y), "y is finite (no null)");
  });
});

// Z and +00:00 same instant collapse within a series / align across series
var iso = [
  { name: "A", points: [{ ts: "2026-01-01T00:00:00+00:00", v: 1 }] },
  { name: "B", points: [{ ts: "2026-01-01T00:00:00Z", v: 10 }] },
];
var isoPacked = pack(iso);
assert(isoPacked.apexSeries[0].data[0].x === isoPacked.apexSeries[1].data[0].x,
  "Z and +00:00 share the same x");

console.log("ok");
