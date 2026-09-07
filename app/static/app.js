/* Action confirmations and the edit modal. */
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

  function signed(n) {
    return (n > 0 ? "+" : "") + n;
  }

  // Changing the standard moves "remaining" with it, one for one, because
  // remaining is derived from the standard rather than stored. A typo in this
  // field therefore shifts the whole stock picture for the item, so the change
  // is spelled out before it is saved. Only a real change to the number asks —
  // renaming an item or toggling "active" saves without a prompt.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.dataset || form.dataset.confirmStandard === undefined) {
      return;
    }
    var field = form.elements.standard_qty;
    if (!field) {
      return;
    }
    var before = parseInt(form.dataset.standard, 10);
    var after = parseInt(field.value, 10);
    if (isNaN(before) || isNaN(after) || before === after) {
      return;
    }
    var message =
      "שינוי תקן — " + form.dataset.sku + " · " + form.dataset.name + "\n\n" +
      "תקן: " + before + " → " + after + "\n" +
      'העמודה "נשאר" בלוח המצב תשתנה ב-' + signed(after - before) + ".\n\n" +
      "לשמור?";
    if (!window.confirm(message)) {
      event.preventDefault();
    }
  });

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
