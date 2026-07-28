(function () {
  function bindDialog(dialog) {
    if (!dialog || dialog.dataset.outsideCloseBound === "1") return;
    dialog.dataset.outsideCloseBound = "1";
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
  }

  function initDialogs(root) {
    (root || document).querySelectorAll(".dialog").forEach(bindDialog);
  }

  initDialogs(document);
  document.body.addEventListener("htmx:afterSwap", function (event) {
    initDialogs(event.target);
  });
})();
