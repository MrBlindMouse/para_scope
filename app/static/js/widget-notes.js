/** Notes widgets: debounced save of textarea body into layout config. */
(function () {
  "use strict";

  var SAVE_DELAY_MS = 10000;

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function saveNotes(el) {
    var id = el.getAttribute("data-widget-id") || "";
    if (!id) return;
    var grid = document.getElementById("dashboard-grid");
    var page = grid ? (grid.getAttribute("data-page") || "") : "";
    var payload = { id: id, text: el.value };
    if (page) payload.page = page;
    fetch("/api/dashboard/notes", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(payload),
    }).catch(function (err) {
      console.error("notes save failed", err);
    });
  }

  function scheduleSave(el) {
    var prev = el._notesSaveTimer;
    if (prev) clearTimeout(prev);
    el._notesSaveTimer = setTimeout(function () {
      el._notesSaveTimer = null;
      saveNotes(el);
    }, SAVE_DELAY_MS);
  }

  function flushSave(el) {
    if (!el || !el._notesSaveTimer) return;
    clearTimeout(el._notesSaveTimer);
    el._notesSaveTimer = null;
    saveNotes(el);
  }

  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!el || !el.getAttribute || !el.hasAttribute("data-notes-widget")) return;
    scheduleSave(el);
  });

  document.addEventListener("blur", function (e) {
    var el = e.target;
    if (!el || !el.getAttribute || !el.hasAttribute("data-notes-widget")) return;
    flushSave(el);
  }, true);

  window.addEventListener("pagehide", function () {
    document.querySelectorAll("[data-notes-widget]").forEach(flushSave);
  });
})();
