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
    fetch("/api/dashboard/notes", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify({ id: id, text: el.value }),
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

  document.addEventListener("input", function (e) {
    var el = e.target;
    if (!el || !el.getAttribute || !el.hasAttribute("data-notes-widget")) return;
    scheduleSave(el);
  });
})();
