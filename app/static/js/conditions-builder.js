/**
 * Rule conditions: visual Field/Operator/Value rows ↔ JSON.
 * Init after HTMX swaps the rule form into #pipeline-dialog.
 */
(function () {
  var OPS = ["equals", "not", "gt", "lt", "contains", "regex"];
  var OP_LABELS = {
    equals: "Equals (=)",
    not: "Not equal (≠)",
    gt: "Greater than (>)",
    lt: "Less than (<)",
    contains: "Contains",
    regex: "Matches pattern (regex)"
  };

  function parseInitial(el) {
    var raw = el.getAttribute("data-initial") || "{}";
    try {
      var v = JSON.parse(raw);
      return v && typeof v === "object" && !Array.isArray(v) ? v : {};
    } catch (e) {
      return {};
    }
  }

  function coerceEquals(raw) {
    var s = String(raw).trim();
    if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
    var lower = s.toLowerCase();
    if (lower === "true" || lower === "on" || lower === "yes") return true;
    if (lower === "false" || lower === "off" || lower === "no") return false;
    return String(raw);
  }

  function coerceNumeric(raw) {
    var n = Number(String(raw).trim());
    return Number.isFinite(n) ? n : String(raw);
  }

  function conditionsToRows(obj) {
    var rows = [];
    Object.keys(obj || {}).forEach(function (field) {
      var matcher = obj[field];
      if (matcher !== null && typeof matcher === "object" && !Array.isArray(matcher)) {
        OPS.slice(1).forEach(function (op) {
          if (Object.prototype.hasOwnProperty.call(matcher, op)) {
            rows.push({ field: field, op: op, value: matcher[op] });
          }
        });
        Object.keys(matcher).forEach(function (k) {
          if (OPS.indexOf(k) === -1) {
            rows.push({ field: field, op: "equals", value: JSON.stringify(matcher[k]) });
          }
        });
      } else {
        rows.push({ field: field, op: "equals", value: matcher });
      }
    });
    return rows;
  }

  function rowsToConditions(rows) {
    var out = {};
    rows.forEach(function (row) {
      var field = (row.field || "").trim();
      if (!field) return;
      var op = row.op || "equals";
      var raw = row.value;
      if (op === "equals") {
        out[field] = coerceEquals(raw == null ? "" : raw);
        return;
      }
      var val;
      if (op === "not") val = coerceEquals(raw == null ? "" : raw);
      else if (op === "gt" || op === "lt") val = coerceNumeric(raw);
      else val = String(raw == null ? "" : raw);
      var existing = out[field];
      if (existing !== null && typeof existing === "object" && !Array.isArray(existing)) {
        existing[op] = val;
      } else {
        var m = {};
        m[op] = val;
        out[field] = m;
      }
    });
    return out;
  }

  function rowEl(row) {
    var wrap = document.createElement("div");
    wrap.className = "conditions-builder__row";

    var field = document.createElement("input");
    field.type = "text";
    field.className = "input conditions-builder__field";
    field.placeholder = "Field";
    field.value = row.field != null ? String(row.field) : "";
    field.setAttribute("aria-label", "Condition field");

    var op = document.createElement("select");
    op.className = "input conditions-builder__op";
    op.setAttribute("aria-label", "Condition operator");
    OPS.forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = OP_LABELS[name] || name;
      if (name === (row.op || "equals")) opt.selected = true;
      op.appendChild(opt);
    });

    var value = document.createElement("input");
    value.type = "text";
    value.className = "input conditions-builder__value";
    value.placeholder = "Value";
    value.value = row.value != null ? String(row.value) : "";
    value.setAttribute("aria-label", "Condition value");

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn--sm conditions-builder__remove";
    remove.setAttribute("aria-label", "Remove condition");
    remove.textContent = "×";

    wrap.appendChild(field);
    wrap.appendChild(op);
    wrap.appendChild(value);
    wrap.appendChild(remove);
    return wrap;
  }

  function readRows(root) {
    var rows = [];
    root.querySelectorAll(".conditions-builder__row").forEach(function (el) {
      rows.push({
        field: el.querySelector(".conditions-builder__field").value,
        op: el.querySelector(".conditions-builder__op").value,
        value: el.querySelector(".conditions-builder__value").value,
      });
    });
    return rows;
  }

  function setHidden(root, obj) {
    var hidden = root.querySelector(".conditions-builder__hidden");
    if (hidden) hidden.value = JSON.stringify(obj);
  }

  function syncEmpty(root) {
    var rows = root.querySelector(".conditions-builder__rows");
    var empty = root.querySelector(".conditions-builder__empty");
    if (!empty || !rows) return;
    empty.hidden = rows.children.length > 0;
  }

  function renderRows(root, rows) {
    var container = root.querySelector(".conditions-builder__rows");
    if (!container) return;
    container.innerHTML = "";
    (rows || []).forEach(function (r) {
      container.appendChild(rowEl(r));
    });
    syncEmpty(root);
  }

  function syncFromVisual(root) {
    setHidden(root, rowsToConditions(readRows(root)));
  }

  function syncFromJson(root) {
    var ta = root.querySelector(".conditions-builder__textarea");
    var raw = (ta && ta.value || "").trim() || "{}";
    try {
      var obj = JSON.parse(raw);
      if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error("not object");
      setHidden(root, obj);
      if (ta) ta.classList.remove("input--invalid");
      return obj;
    } catch (e) {
      if (ta) ta.classList.add("input--invalid");
      return null;
    }
  }

  function isJsonMode(root) {
    var jsonPanel = root.querySelector(".conditions-builder__json");
    return jsonPanel && !jsonPanel.hidden;
  }

  function setMode(root, jsonMode) {
    var visual = root.querySelector(".conditions-builder__visual");
    var jsonPanel = root.querySelector(".conditions-builder__json");
    if (!visual || !jsonPanel) return;
    if (jsonMode) {
      syncFromVisual(root);
      var hidden = root.querySelector(".conditions-builder__hidden");
      var ta = root.querySelector(".conditions-builder__textarea");
      if (ta && hidden) {
        try {
          ta.value = JSON.stringify(JSON.parse(hidden.value || "{}"), null, 2);
        } catch (e) {
          ta.value = hidden.value || "{}";
        }
      }
      visual.hidden = true;
      jsonPanel.hidden = false;
    } else {
      var obj = syncFromJson(root);
      if (obj === null) return;
      renderRows(root, conditionsToRows(obj));
      visual.hidden = false;
      jsonPanel.hidden = true;
    }
  }

  function bind(root) {
    if (root.dataset.conditionsBound === "1") return;
    root.dataset.conditionsBound = "1";

    var initial = parseInitial(root);
    setHidden(root, initial);
    renderRows(root, conditionsToRows(initial));
    var ta = root.querySelector(".conditions-builder__textarea");
    if (ta) ta.value = JSON.stringify(initial, null, 2);

    root.addEventListener("click", function (evt) {
      var t = evt.target;
      if (t.closest(".conditions-builder__add")) {
        evt.preventDefault();
        var container = root.querySelector(".conditions-builder__rows");
        container.appendChild(rowEl({ field: "", op: "equals", value: "" }));
        syncEmpty(root);
        syncFromVisual(root);
        return;
      }
      if (t.closest(".conditions-builder__remove")) {
        evt.preventDefault();
        var row = t.closest(".conditions-builder__row");
        if (row) row.remove();
        syncEmpty(root);
        syncFromVisual(root);
        return;
      }
      if (t.closest(".conditions-builder__toggle")) {
        evt.preventDefault();
        setMode(root, !isJsonMode(root));
      }
    });

    root.addEventListener("input", function () {
      if (isJsonMode(root)) syncFromJson(root);
      else syncFromVisual(root);
    });
    root.addEventListener("change", function () {
      if (isJsonMode(root)) syncFromJson(root);
      else syncFromVisual(root);
    });

    var form = root.closest("form");
    if (form) {
      form.addEventListener("submit", function () {
        if (isJsonMode(root)) {
          if (syncFromJson(root) === null) {
            // leave invalid JSON in place; server will reject
            var ta2 = root.querySelector(".conditions-builder__textarea");
            var hidden = root.querySelector(".conditions-builder__hidden");
            if (hidden && ta2) hidden.value = ta2.value;
          }
        } else {
          syncFromVisual(root);
        }
      });
    }
  }

  window.initConditionsBuilders = function (scope) {
    var root = scope || document;
    var nodes = root.querySelectorAll
      ? root.querySelectorAll(".conditions-builder")
      : [];
    Array.prototype.forEach.call(nodes, bind);
  };

  document.addEventListener("DOMContentLoaded", function () {
    window.initConditionsBuilders(document);
  });

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (target) window.initConditionsBuilders(target);
  });
})();
