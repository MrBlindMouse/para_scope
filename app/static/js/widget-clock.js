/** Live ticking for dashboard clock widgets. */
(function () {
  "use strict";

  var intervalId = null;

  function browserTimeZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (e) {
      return "";
    }
  }

  function resolveZone(el) {
    var mode = el.getAttribute("data-timezone-mode") || "app";
    if (mode === "browser") {
      return browserTimeZone() || el.getAttribute("data-server-timezone") || "UTC";
    }
    return el.getAttribute("data-timezone") || el.getAttribute("data-server-timezone") || "UTC";
  }

  function clockParts(date, timeZone) {
    try {
      var parts = new Intl.DateTimeFormat("en-GB-u-hc-h23", {
        timeZone: timeZone,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      }).formatToParts(date);
      var map = {};
      parts.forEach(function (part) {
        if (part.type !== "literal") map[part.type] = part.value;
      });
      return {
        hour: parseInt(map.hour || "0", 10),
        minute: parseInt(map.minute || "0", 10),
        second: parseInt(map.second || "0", 10)
      };
    } catch (e) {
      return {hour: date.getUTCHours(), minute: date.getUTCMinutes(), second: date.getUTCSeconds()};
    }
  }

  function formatTime(date, timeZone, hourFormat, showSeconds) {
    var opts = {
      timeZone: timeZone,
      hour: "2-digit",
      minute: "2-digit",
      hour12: hourFormat === "12"
    };
    if (showSeconds) opts.second = "2-digit";
    return new Intl.DateTimeFormat(undefined, opts).format(date);
  }

  function formatDate(date, timeZone) {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: timeZone,
      weekday: "short",
      day: "2-digit",
      month: "short",
      year: "numeric"
    }).format(date);
  }

  function formatOffset(date, timeZone) {
    try {
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: timeZone,
        timeZoneName: "shortOffset"
      }).formatToParts(date);
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") {
          return parts[i].value.replace("GMT", "UTC");
        }
      }
    } catch (e) {}
    return "";
  }

  function renderPrimary(el, now) {
    var timeZone = resolveZone(el);
    var showSeconds = el.getAttribute("data-show-seconds") === "1";
    var showDate = el.getAttribute("data-show-date") === "1";
    var showTimezone = el.getAttribute("data-show-timezone") !== "0";
    var hourFormat = el.getAttribute("data-hour-format") || "24";
    var timeEl = el.querySelector("[data-clock-time]");
    var dateEl = el.querySelector("[data-clock-date]");
    var offsetEl = el.querySelector("[data-clock-offset]");
    var labelEl = el.querySelector("[data-clock-label]");
    if (timeEl) timeEl.textContent = formatTime(now, timeZone, hourFormat, showSeconds);
    if (dateEl && showDate) dateEl.textContent = formatDate(now, timeZone);
    if (showTimezone) {
      if (offsetEl) offsetEl.textContent = formatOffset(now, timeZone);
      if (labelEl && el.getAttribute("data-timezone-mode") === "browser") {
        labelEl.textContent = timeZone || "Browser time";
      }
    }

    if (el.getAttribute("data-clock-display") === "analog") {
      var parts = clockParts(now, timeZone);
      var hourAngle = ((parts.hour % 12) + (parts.minute / 60) + (parts.second / 3600)) * 30;
      var minuteAngle = (parts.minute + (parts.second / 60)) * 6;
      var secondAngle = parts.second * 6;
      var hourHand = el.querySelector("[data-clock-hour]");
      var minuteHand = el.querySelector("[data-clock-minute]");
      var secondHand = el.querySelector("[data-clock-second]");
      if (hourHand) hourHand.style.transform = "rotate(" + hourAngle + "deg)";
      if (minuteHand) minuteHand.style.transform = "rotate(" + minuteAngle + "deg)";
      if (secondHand) secondHand.style.transform = "rotate(" + secondAngle + "deg)";
    }
  }

  function renderWorld(el, now) {
    var showSeconds = el.getAttribute("data-show-seconds") === "1";
    var showDate = el.getAttribute("data-show-date") === "1";
    var showTimezone = el.getAttribute("data-show-timezone") !== "0";
    var hourFormat = el.getAttribute("data-hour-format") || "24";
    el.querySelectorAll("[data-clock-row]").forEach(function (row) {
      var timeZone = row.getAttribute("data-timezone") || "UTC";
      var timeEl = row.querySelector("[data-clock-time]");
      var dateEl = row.querySelector("[data-clock-date]");
      var offsetEl = row.querySelector("[data-clock-offset]");
      if (timeEl) timeEl.textContent = formatTime(now, timeZone, hourFormat, showSeconds);
      if (dateEl && showDate) dateEl.textContent = formatDate(now, timeZone);
      if (offsetEl && showTimezone) offsetEl.textContent = formatOffset(now, timeZone);
    });
  }

  function renderAll() {
    var now = new Date();
    document.querySelectorAll("[data-clock-widget]").forEach(function (el) {
      if (el.getAttribute("data-clock-display") === "world_clock") renderWorld(el, now);
      else renderPrimary(el, now);
    });
  }

  function start() {
    renderAll();
    if (intervalId) clearInterval(intervalId);
    intervalId = setInterval(renderAll, 1000);
  }

  document.addEventListener("DOMContentLoaded", start);
  document.body.addEventListener("htmx:afterSwap", renderAll);
})();
