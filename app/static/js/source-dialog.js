/**
 * Pipeline source create/edit dialog: type sections + delayed tooltips.
 * Invoked after HTMX swaps content into #pipeline-dialog.
 */
(function () {
  var TIP_DELAY_MS = 3000;

  function ensureBubble() {
    var bubble = document.getElementById("field-tip-bubble");
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.id = "field-tip-bubble";
      bubble.className = "field-tip-bubble";
      bubble.setAttribute("role", "tooltip");
      bubble.setAttribute("popover", "manual");
      document.body.appendChild(bubble);
    } else if (!bubble.hasAttribute("popover")) {
      bubble.setAttribute("popover", "manual");
    }
    return bubble;
  }

  function initTips(form) {
    var bubble = ensureBubble();
    var canPopover = typeof bubble.showPopover === "function";
    var tipTimer = null;
    var activeTip = null;
    var dialog = document.getElementById("pipeline-dialog");

    function hideTip() {
      if (tipTimer) {
        clearTimeout(tipTimer);
        tipTimer = null;
      }
      activeTip = null;
      bubble.textContent = "";
      if (canPopover) {
        try {
          if (bubble.matches(":popover-open")) bubble.hidePopover();
        } catch (e) {}
      }
      bubble.hidden = true;
    }

    function placeTip(btn) {
      var text = btn.getAttribute("data-tip") || "";
      if (!text) return;
      bubble.textContent = text;
      bubble.hidden = false;
      if (canPopover) {
        try {
          if (!bubble.matches(":popover-open")) bubble.showPopover();
        } catch (e) {}
      }
      var rect = btn.getBoundingClientRect();
      var gap = 6;
      var bw = bubble.offsetWidth;
      var bh = bubble.offsetHeight;
      var left = rect.left + rect.width / 2 - bw / 2;
      var top = rect.bottom + gap;
      if (top + bh > window.innerHeight - 8 && rect.top - gap - bh > 8) {
        top = rect.top - gap - bh;
      }
      left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
      top = Math.max(8, Math.min(top, window.innerHeight - bh - 8));
      bubble.style.left = left + "px";
      bubble.style.top = top + "px";
    }

    function showTipSoon(btn) {
      hideTip();
      activeTip = btn;
      tipTimer = setTimeout(function () {
        tipTimer = null;
        if (activeTip === btn) placeTip(btn);
      }, TIP_DELAY_MS);
    }

    form.querySelectorAll(".field-tip").forEach(function (btn) {
      btn.addEventListener("mouseenter", function () { showTipSoon(btn); });
      btn.addEventListener("mouseleave", hideTip);
      btn.addEventListener("focus", function () { showTipSoon(btn); });
      btn.addEventListener("blur", hideTip);
    });
    if (dialog && !dialog.dataset.tipCloseBound) {
      dialog.dataset.tipCloseBound = "1";
      dialog.addEventListener("close", hideTip);
    }
  }

  function initTypeToggle(form) {
    var typeSel = form.querySelector("#source-type");
    var schedType = form.querySelector("#sched-type");
    var pollCategory = form.querySelector("#poll-category");
    var handlerType = form.querySelector("#handler-type");
    var webhookFields = form.querySelector("#webhook-secret-fields");
    var scheduleFields = form.querySelector("#schedule-fields");
    var intervalFields = form.querySelector("#interval-fields");
    var cronFields = form.querySelector("#cron-fields");
    if (!typeSel || !webhookFields || !scheduleFields) return;

    function syncHandlerOptions() {
      if (!pollCategory || !handlerType) return;
      var selected = pollCategory.value;
      var firstVisible = null;
      Array.prototype.forEach.call(handlerType.options, function (option) {
        var visible = option.getAttribute("data-category") === selected;
        option.hidden = !visible;
        option.disabled = !visible;
        if (visible && !firstVisible) firstVisible = option.value;
      });
      if (handlerType.selectedOptions.length && !handlerType.selectedOptions[0].hidden) {
        return;
      }
      if (firstVisible) handlerType.value = firstVisible;
    }

    function syncHandlerPanels() {
      if (!handlerType) return;
      form.querySelectorAll("[data-handler-fields]").forEach(function (panel) {
        var active = panel.getAttribute("data-handler-fields") === handlerType.value;
        panel.hidden = !active;
        Array.prototype.forEach.call(panel.querySelectorAll("input, select, textarea"), function (inp) {
          inp.disabled = !active;
        });
      });
    }

    function toggleSchedType() {
      if (!schedType || !intervalFields || !cronFields) return;
      if (schedType.value === "cron") {
        intervalFields.style.display = "none";
        cronFields.style.display = "";
      } else {
        intervalFields.style.display = "";
        cronFields.style.display = "none";
      }
    }

    function toggleSourceType() {
      if (typeSel.value === "poll") {
        webhookFields.style.display = "none";
        scheduleFields.style.display = "";
      } else {
        webhookFields.style.display = "";
        scheduleFields.style.display = "none";
      }
    }

    typeSel.addEventListener("change", toggleSourceType);
    if (schedType) schedType.addEventListener("change", toggleSchedType);
    if (pollCategory) {
      pollCategory.addEventListener("change", function () {
        syncHandlerOptions();
        syncHandlerPanels();
      });
    }
    if (handlerType) {
      handlerType.addEventListener("change", syncHandlerPanels);
    }
    syncHandlerOptions();
    syncHandlerPanels();
    toggleSourceType();
    toggleSchedType();
  }

  function initFieldTypeToggle(form) {
    var typeSel = form.querySelector("#field-type-select");
    var panels = form.querySelectorAll(".field-type-params");
    if (!typeSel || !panels.length) return;

    function currentName() {
      var active = form.querySelector(".field-type-params:not([hidden]) .field-name-input");
      return active ? active.value : "";
    }

    function sync() {
      var typed = currentName();
      var ft = typeSel.disabled
        ? (form.querySelector('input[type="hidden"][name="field_type"]') || typeSel).value
        : typeSel.value;
      panels.forEach(function (panel) {
        var match = panel.id === "field-params-" + ft;
        panel.hidden = !match;
        Array.prototype.forEach.call(
          panel.querySelectorAll("input, select, textarea"),
          function (inp) { inp.disabled = !match; }
        );
        var nameInp = panel.querySelector(".field-name-input");
        if (nameInp) {
          if (match && typed) nameInp.value = typed;
          nameInp.required = match;
        }
      });
    }

    if (!typeSel.disabled) {
      typeSel.addEventListener("change", sync);
    }
    sync();
  }

  window.initPipelineSourceForm = function (root) {
    var form = null;
    if (root && root.tagName === "FORM") {
      form = root;
    } else if (root && root.querySelector) {
      form = root.querySelector(".dialog__form") || root.querySelector("form");
    } else {
      form = root;
    }
    if (!form || form.tagName !== "FORM") return;
    initTypeToggle(form);
    initFieldTypeToggle(form);
    initTips(form);
  };

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (target && target.id === "pipeline-dialog") {
      window.initPipelineSourceForm(target);
    }
  });
})();
