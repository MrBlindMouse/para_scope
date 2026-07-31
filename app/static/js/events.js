/** Events page: SSE live tail (page 1 only). */
(function () {
  "use strict";

  var MAX_ROWS = 50;
  var es = null;

  function wrap() {
    return document.getElementById("events-table-wrap");
  }

  function tbody() {
    return document.getElementById("events-tbody");
  }

  function closeStream() {
    if (es) {
      es.close();
      es = null;
    }
  }

  function trimRows(tb) {
    while (tb.children.length > MAX_ROWS) {
      tb.removeChild(tb.lastElementChild);
    }
  }

  function hasRow(tb, id) {
    return !!tb.querySelector('tr[data-event-id="' + id + '"]');
  }

  function prependRow(html) {
    var tb = tbody();
    if (!tb || !html) return;
    var tmp = document.createElement("tbody");
    tmp.innerHTML = html.trim();
    var row = tmp.firstElementChild;
    if (!row) return;
    var id = row.getAttribute("data-event-id");
    if (id && hasRow(tb, id)) return;
    tb.insertBefore(row, tb.firstChild);
    trimRows(tb);
    var empty = document.getElementById("events-empty");
    if (empty) empty.hidden = true;
  }

  function connect() {
    closeStream();
    var w = wrap();
    if (!w) return;
    var url = w.getAttribute("data-events-stream-url");
    if (!url) return;
    var tb = tbody();
    var after = "0";
    if (tb) {
      var first = tb.querySelector("tr[data-event-id]");
      if (first) after = first.getAttribute("data-event-id") || "0";
    }
    var sep = url.indexOf("?") >= 0 ? "&" : "?";
    es = new EventSource(url + sep + "after=" + encodeURIComponent(after));
    es.addEventListener("event", function (ev) {
      try {
        prependRow(JSON.parse(ev.data));
      } catch (err) {
        console.error("event stream parse failed", err);
      }
    });
    es.onerror = function () {
      // Native EventSource reconnects automatically.
    };
  }

  document.addEventListener("DOMContentLoaded", connect);
  document.body.addEventListener("htmx:afterSwap", function (ev) {
    var target = ev.detail && ev.detail.target;
    if (target && target.id === "events-table-wrap") {
      connect();
    }
  });
})();
