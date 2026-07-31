/** Dashboard Triggers widget: fire webhook event types or poll sources. */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function setBusy(btn, busy) {
    if (busy) {
      btn.disabled = true;
      btn.dataset.busy = "1";
    } else {
      btn.disabled = false;
      delete btn.dataset.busy;
    }
  }

  function fire(btn) {
    if (btn.dataset.busy) return;
    var kind = btn.getAttribute("data-trigger-kind") || "";
    var sourceId = parseInt(btn.getAttribute("data-trigger-source-id") || "", 10);
    if (!kind || !sourceId) return;
    var body = { kind: kind, source_id: sourceId };
    if (kind === "webhook") {
      var etId = parseInt(btn.getAttribute("data-trigger-event-type-id") || "", 10);
      if (!etId) return;
      body.event_type_id = etId;
      var raw = btn.getAttribute("data-trigger-payload");
      if (raw) {
        try {
          var parsed = JSON.parse(raw);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            body.payload = parsed;
          }
        } catch (e) {
          /* ignore bad attribute */
        }
      }
    }
    setBusy(btn, true);
    fetch("/api/dashboard/trigger", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(body),
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (data) {
            throw new Error((data && data.error) || ("Trigger failed (" + resp.status + ")"));
          });
        }
      })
      .catch(function (err) {
        console.error("trigger failed", err);
        window.alert(err.message || "Trigger failed");
      })
      .finally(function () {
        setBusy(btn, false);
      });
  }

  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest("[data-trigger-btn]") : null;
    if (!btn) return;
    e.preventDefault();
    fire(btn);
  });
})();
