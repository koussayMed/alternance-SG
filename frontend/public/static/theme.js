// Light / dark theme. Loaded in <head>, before rendering, to avoid a flash of the wrong colours.
// Separate file because the CSP forbids inline scripts. Without a saved choice, follows the system.
(function () {
  "use strict";
  var KEY = "meteo-theme";
  var root = document.documentElement;
  var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) {
      return null; // storage blocked: follow the system
    }
  }

  function current() {
    return root.dataset.theme || (systemDark && systemDark.matches ? "dark" : "light");
  }

  var choice = saved();
  if (choice) root.dataset.theme = choice;

  function syncButton() {
    var btn = document.getElementById("theme-btn");
    if (!btn) return;
    var dark = current() === "dark";
    // The icon shows the mode you switch to
    btn.querySelector("use").setAttribute("href", dark ? "#i-sun" : "#i-moon");
    var label = dark ? "Passer en mode clair" : "Passer en mode sombre";
    btn.setAttribute("aria-label", label);
    btn.title = label;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-btn");
    if (!btn) return;
    syncButton();
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem(KEY, next); } catch (e) { /* choice kept for this page only */ }
      syncButton();
    });
  });

  // The system theme changes (e.g. automatic at night) and no choice is saved
  if (systemDark && systemDark.addEventListener) {
    systemDark.addEventListener("change", syncButton);
  }
})();
