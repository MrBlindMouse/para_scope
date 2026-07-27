/** GridStack dashboard: edit mode + persist geometry. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function init() {
    var el = document.getElementById("dashboard-grid");
    if (!el || typeof GridStack === "undefined") return;

    var columnMax = parseInt(el.getAttribute("data-gs-column") || "12", 10);
    var columnWidth = parseInt(el.getAttribute("data-gs-column-width") || "40", 10);
    var liveMax = parseInt(el.getAttribute("data-gs-column-live-max") || "96", 10);
    var cellHeight = parseInt(el.getAttribute("data-gs-cell-height") || "40", 10);
    var margin = parseInt(el.getAttribute("data-gs-margin") || "6", 10);
    var stackBelow = parseInt(el.getAttribute("data-gs-stack-below") || "768", 10);

    // Prepare in the full live coordinate space so ultrawide right-edge x/w are not
    // clamped into the design 36-col grid before applyResponsiveColumns runs.
    var grid = GridStack.init({
      column: liveMax,
      cellHeight: cellHeight,
      margin: margin,
      float: false,
      staticGrid: true,
      handle: ".card__header",
    }, el);

    var editing = false;
    var saveTimer = null;
    var resizeTimer = null;
    var toggle = document.getElementById("dashboard-edit-toggle");

    function atDesignWidth() {
      // Standard width or wider: enough live columns to show design units at ~40px/cell.
      return grid.getColumn() >= columnMax;
    }

    function collectGeometry() {
      var items = [];
      el.querySelectorAll(".grid-stack-item").forEach(function (node) {
        var id = node.getAttribute("gs-id");
        if (!id) return;
        var n = node.gridstackNode || {};
        items.push({
          id: id,
          x: n.x != null ? n.x : parseInt(node.getAttribute("gs-x") || "0", 10),
          y: n.y != null ? n.y : parseInt(node.getAttribute("gs-y") || "0", 10),
          w: n.w != null ? n.w : parseInt(node.getAttribute("gs-w") || "6", 10),
          h: n.h != null ? n.h : parseInt(node.getAttribute("gs-h") || "3", 10),
        });
      });
      return items;
    }

    function saveLayout() {
      if (!atDesignWidth()) return;
      var widgets = collectGeometry();
      fetch("/api/dashboard/layout", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken(),
        },
        body: JSON.stringify({ widgets: widgets }),
      }).catch(function (err) {
        console.error("dashboard layout save failed", err);
      });
    }

    function scheduleSave() {
      if (!editing) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(saveLayout, 400);
    }

    function setEditing(on, opts) {
      var canSave = !opts || opts.save !== false;
      if (on && !atDesignWidth()) return;

      editing = !!on;
      grid.setStatic(!editing);
      el.classList.toggle("dashboard-grid--editing", editing);
      if (toggle) {
        toggle.setAttribute("aria-pressed", editing ? "true" : "false");
        toggle.title = editing ? "Lock layout" : "Unlock layout";
        toggle.setAttribute("aria-label", editing ? "Lock layout" : "Unlock layout");
      }
      if (!editing) {
        clearTimeout(saveTimer);
        if (canSave) saveLayout();
      }
    }

    function syncEditAvailability() {
      var canEdit = atDesignWidth();
      if (toggle) {
        toggle.disabled = !canEdit;
        toggle.setAttribute("aria-disabled", canEdit ? "false" : "true");
        if (!canEdit) {
          toggle.title = "Layout editing needs full design width";
          toggle.setAttribute("aria-label", "Layout editing unavailable at this width");
        } else if (!editing) {
          toggle.title = "Unlock layout";
          toggle.setAttribute("aria-label", "Unlock layout");
        }
      }
      if (!canEdit && editing) {
        setEditing(false, { save: false });
      }
    }

    function applyResponsiveColumns() {
      // Stack mode uses viewport width so GRID_STACK_BELOW matches what DevTools shows.
      // Cell count uses the grid's clientWidth (content box after page padding).
      var viewport = window.innerWidth || document.documentElement.clientWidth || el.clientWidth;
      var gridWidth = el.clientWidth;
      var next;
      var layout;
      if (viewport <= stackBelow) {
        next = 1;
        layout = "list";
      } else {
        // Add/remove columns vs design standard; keep absolute x/w (do not rescale spans).
        next = Math.min(Math.round(gridWidth / columnWidth) || 1, liveMax);
        layout = "none";
      }
      if (grid.getColumn() !== next) {
        grid.column(next, layout);
      }
      syncEditAvailability();
    }

    function scheduleResponsiveColumns() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(applyResponsiveColumns, 50);
    }

    grid.on("change", function () {
      syncEditAvailability();
      scheduleSave();
    });

    if (toggle) {
      toggle.addEventListener("click", function () {
        if (toggle.disabled) return;
        setEditing(!editing);
      });
    }

    applyResponsiveColumns();
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(scheduleResponsiveColumns).observe(el);
    }
    window.addEventListener("resize", scheduleResponsiveColumns);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
