(function () {
  var persistedState = Object.create(null);

  function closeActionMenu(menu) {
    if (menu) menu.removeAttribute("open");
  }

  function closeOpenActionMenus(target) {
    document.querySelectorAll(".pipeline-action-menu[open]").forEach(function (menu) {
      if (target && menu.contains(target)) return;
      closeActionMenu(menu);
    });
  }

  function setExpanded(button, expanded) {
    var targetId = button.getAttribute("aria-controls");
    if (!targetId) return;

    var panel = document.getElementById(targetId);
    if (!panel) return;

    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    panel.hidden = !expanded;

    var key = button.getAttribute("data-disclosure-key");
    if (key) persistedState[key] = expanded;
  }

  function applyPersistedState(root) {
    var scope = root || document;
    var buttons = scope.querySelectorAll("[data-disclosure-toggle][data-disclosure-key]");
    buttons.forEach(function (button) {
      var key = button.getAttribute("data-disclosure-key");
      if (!key || persistedState[key] == null) return;
      setExpanded(button, persistedState[key]);
    });
  }

  document.body.addEventListener("click", function (event) {
    var button = event.target.closest("[data-disclosure-toggle]");
    if (button) {
      var expanded = button.getAttribute("aria-expanded") === "true";
      setExpanded(button, !expanded);
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
