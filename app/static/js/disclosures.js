(function () {
  var persistedState = Object.create(null);
  var RESIZE_MS = 200;

  function closeActionMenu(menu) {
    if (menu) menu.removeAttribute("open");
  }

  function closeOpenActionMenus(target) {
    document.querySelectorAll(".pipeline-action-menu[open]").forEach(function (menu) {
      if (target && menu.contains(target)) return;
      closeActionMenu(menu);
    });
  }

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function clearCardResize(card) {
    if (!card) return;
    card.classList.remove("is-resizing");
    card.style.width = "";
    card.style.height = "";
    card.style.overflow = "";
  }

  function flipCardSize(card, from, to) {
    if (!card) return;
    var dw = Math.abs(from.width - to.width);
    var dh = Math.abs(from.height - to.height);
    if (dw < 0.5 && dh < 0.5) return;

    clearCardResize(card);
    card.style.width = from.width + "px";
    card.style.height = from.height + "px";
    card.style.overflow = "hidden";
    void card.offsetWidth;
    card.classList.add("is-resizing");
    card.style.width = to.width + "px";
    card.style.height = to.height + "px";

    var done = false;
    function finish() {
      if (done) return;
      done = true;
      card.removeEventListener("transitionend", onEnd);
      clearTimeout(fallback);
      clearCardResize(card);
    }
    function onEnd(event) {
      if (event.target !== card) return;
      if (event.propertyName !== "width" && event.propertyName !== "height") return;
      finish();
    }
    card.addEventListener("transitionend", onEnd);
    var fallback = setTimeout(finish, RESIZE_MS + 50);
  }

  function setExpanded(button, expanded, animate) {
    var targetId = button.getAttribute("aria-controls");
    if (!targetId) return;

    var panel = document.getElementById(targetId);
    if (!panel) return;

    var card = button.closest(".card");
    var from = null;
    if (animate && card && !prefersReducedMotion()) {
      from = card.getBoundingClientRect();
    }

    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    panel.hidden = !expanded;

    var key = button.getAttribute("data-disclosure-key");
    if (key) persistedState[key] = expanded;

    if (from && card) {
      var to = card.getBoundingClientRect();
      flipCardSize(card, from, to);
    }
  }

  function applyPersistedState(root) {
    var scope = root || document;
    var buttons = scope.querySelectorAll("[data-disclosure-toggle][data-disclosure-key]");
    buttons.forEach(function (button) {
      var key = button.getAttribute("data-disclosure-key");
      if (!key || persistedState[key] == null) return;
      setExpanded(button, persistedState[key], false);
    });
  }

  document.body.addEventListener("click", function (event) {
    var button = event.target.closest("[data-disclosure-toggle]");
    if (button) {
      var expanded = button.getAttribute("aria-expanded") === "true";
      setExpanded(button, !expanded, true);
      return;
    }

    var menuItem = event.target.closest(".pipeline-action-menu__item");
    if (menuItem && !menuItem.closest(".pipeline-action-menu__form")) {
      closeActionMenu(menuItem.closest(".pipeline-action-menu"));
    }

    closeOpenActionMenus(event.target);
  });

  document.body.addEventListener("submit", function (event) {
    var form = event.target.closest(".pipeline-action-menu__form");
    if (!form) return;
    closeActionMenu(form.closest(".pipeline-action-menu"));
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    applyPersistedState(event.target);
  });

  applyPersistedState(document);
})();
