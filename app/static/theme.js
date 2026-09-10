/* Light or dark, remembered per browser.

   This file is loaded synchronously from <head>, so the theme is decided
   before the first paint — a deferred script would show a flash of the wrong
   colours on every page load. */
(function () {
  "use strict";

  var KEY = "theme";
  var DARK = "מעבר למצב בהיר";
  var LIGHT = "מעבר למצב כהה";
  var root = document.documentElement;
  var system = window.matchMedia("(prefers-color-scheme: dark)");

  // A blocked localStorage (private browsing, cookies off) is not an error —
  // the theme simply falls back to the system preference for that visit.
  function saved() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "dark" || value === "light" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function apply(theme) {
    root.dataset.theme = theme;
    var toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) {
      // The glyph comes from CSS; only the wording is set here.
      toggle.title = theme === "dark" ? DARK : LIGHT;
      toggle.setAttribute("aria-label", toggle.title);
    }
  }

  apply(saved() || (system.matches ? "dark" : "light"));

  // The system keeps steering the page until someone chooses here.
  if (typeof system.addEventListener === "function") {
    system.addEventListener("change", function (event) {
      if (!saved()) {
        apply(event.matches ? "dark" : "light");
      }
    });
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest || !event.target.closest("[data-theme-toggle]")) {
      return;
    }
    var theme = root.dataset.theme === "dark" ? "light" : "dark";
    try {
      window.localStorage.setItem(KEY, theme);
    } catch (error) {
      // Not remembered for next time, but the page still switches now.
    }
    apply(theme);
  });

  // The button does not exist yet when this runs — label it once it does.
  document.addEventListener("DOMContentLoaded", function () {
    apply(root.dataset.theme);
  });
})();
