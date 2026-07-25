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

    var column = parseInt(el.getAttribute("data-gs-column") || "12", 10);
    var cellHeight = parseInt(el.getAttribute("data-gs-cell-height") || "40", 10);
    var margin = parseInt(el.getAttribute("data-gs-margin") || "6", 10);
    var grid = GridStack.init({
      column: column,
      cellHeight: cellHeight,
      margin: margin,
      float: false,
      staticGrid: true,
      disableOneColumnMode: true,
      handle: ".card__header",
    }, el);

    var editing = false;
    var saveTimer = null;
    var toggle = document.getElementById("dashboard-edit-toggle");

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

    grid.on("change", scheduleSave);

    function setEditing(on) {
      editing = !!on;
      grid.setStatic(!editing);
      el.classList.toggle("dashboard-grid--editing", editing);
      if (toggle) {
        toggle.setAttribute("aria-pressed", editing ? "true" : "false");
        toggle.title = editing ? "Layout Editable" : "Layout Locked";
        toggle.setAttribute("aria-label", editing ? "Layout Editable" : "Layout Locked");
      }
      if (!editing) {
        clearTimeout(saveTimer);
        saveLayout();
      }
    }

    if (toggle) {
      toggle.addEventListener("click", function () {
        setEditing(!editing);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
