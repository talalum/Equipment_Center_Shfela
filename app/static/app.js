/* Action confirmations, the import comparison, and the edit modal. */
(function () {
  "use strict";

  // Every form with data-confirm asks for confirmation before submitting.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.dataset ? form.dataset.confirm : null;
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  // The import comparison is rendered with the open attribute so that it works
  // without JavaScript too. Where showModal is available it is reopened as a
  // real modal, which brings the backdrop and traps the keyboard inside it.
  var importDialog = document.getElementById("import-dialog");
  if (importDialog && typeof importDialog.showModal === "function") {
    importDialog.close();
    importDialog.showModal();
    // Esc must not slip out of a comparison waiting for a decision: closing it
    // would leave the import waiting with nothing on screen to say so.
    importDialog.addEventListener("cancel", function (event) {
      event.preventDefault();
    });
  }

  var dialog = document.getElementById("edit-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return;
  }

  var form = document.getElementById("edit-form");
  var title = document.getElementById("ed-title");
  var standardEl = document.getElementById("ed-standard");
  var remainingEl = document.getElementById("ed-remaining");
  var qty = document.getElementById("ed-qty");
  var preview = document.getElementById("ed-preview");
  var remaining = 0;
  var standard = 0;

  function refreshPreview() {
    if (qty.value === "") {
      preview.textContent = "";
      preview.classList.remove("bad");
      return;
    }
    var actual = parseInt(qty.value, 10);
    if (isNaN(actual)) {
      preview.textContent = "";
      return;
    }
    var delta = actual - remaining;
    var shortage = Math.max(0, standard - actual);
    if (delta === 0) {
      preview.textContent = "← אין שינוי, לא תירשם תנועה.";
    } else {
      preview.textContent =
        "← ייווצר הפרש של " + (delta > 0 ? "+" : "") + delta + ", החוסר יהיה " + shortage + ".";
    }
    preview.classList.toggle("bad", shortage > 0);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-edit]");
    if (!button) {
      return;
    }
    remaining = parseInt(button.dataset.remaining, 10);
    standard = parseInt(button.dataset.standard, 10);
    form.action = "/items/" + button.dataset.item + "/edit";
    title.textContent = button.dataset.sku + " · " + button.dataset.name;
    standardEl.textContent = standard;
    remainingEl.textContent = remaining;
    qty.value = remaining;
    refreshPreview();
    dialog.showModal();
    qty.focus();
    qty.select();
  });

  qty.addEventListener("input", refreshPreview);
  document.getElementById("ed-cancel").addEventListener("click", function () {
    dialog.close();
  });
})();
