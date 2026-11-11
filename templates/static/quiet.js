// The quiet board's only script: a tooltip for chart marks, the light/dark switch and the test
// table's groups. Everything it shows is also in each chart's <title> elements and table twin, and
// without it every group of the test table stays open.
(function () {
  "use strict";

  var tip = null;
  function show(mark, x, y) {
    var value = mark.getAttribute("data-tip");
    if (!value) return;
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tip";
      tip.setAttribute("role", "status");
      document.body.appendChild(tip);
    }
    tip.textContent = "";
    var b = document.createElement("b");
    b.textContent = value;
    var span = document.createElement("span");
    span.textContent = mark.getAttribute("data-tip-label") || "";
    tip.appendChild(b);
    tip.appendChild(span);
    tip.style.display = "block";
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(x + 14, window.innerWidth - w - 8);
    var top = y - h - 12 < 8 ? y + 16 : y - h - 12;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function hide() { if (tip) tip.style.display = "none"; }

  document.addEventListener("pointermove", function (event) {
    var mark = event.target.closest && event.target.closest("[data-tip]");
    if (mark) show(mark, event.clientX, event.clientY); else hide();
  });
  document.addEventListener("focusin", function (event) {
    var mark = event.target.closest && event.target.closest("[data-tip]");
    if (!mark) return hide();
    var r = mark.getBoundingClientRect();
    show(mark, r.left + r.width / 2, r.top);
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true });

  // Theme: follows the system until the reader picks one; the choice is remembered per browser.
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("quiet-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* storage unavailable: the system setting applies */ }
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest(".theme-toggle");
    if (!button) return;
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("quiet-theme", next); } catch (e) { /* not remembered */ }
  });

  // Test table: a group's members follow its <details>; collapsed only once this script runs.
  document.querySelectorAll("details[data-group]").forEach(function (details) {
    var members = document.querySelector('tbody[data-members="' + details.getAttribute("data-group") + '"]');
    if (!members) return;
    members.hidden = !details.open;
    details.addEventListener("toggle", function () { members.hidden = !details.open; });
  });

  // Categories: a tab bar over its panels. Without this script every panel shows, stacked and linked.
  document.querySelectorAll('[role="tablist"]').forEach(function (list) {
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute("aria-controls")); });
    function select(i, focus) {
      tabs.forEach(function (t, j) {
        var on = j === i;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        if (panels[j]) panels[j].hidden = !on;
      });
      if (focus && tabs[i]) tabs[i].focus();
    }
    var start = 0;
    tabs.forEach(function (t, j) { if (t.getAttribute("aria-selected") === "true") start = j; });
    select(start, false);
    list.addEventListener("click", function (event) {
      var tab = event.target.closest && event.target.closest('[role="tab"]');
      if (tab && tabs.indexOf(tab) >= 0) select(tabs.indexOf(tab), false);
    });
    list.addEventListener("keydown", function (event) {
      var i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      var n = tabs.length;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") { select((i + 1) % n, true); event.preventDefault(); }
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { select((i - 1 + n) % n, true); event.preventDefault(); }
      else if (event.key === "Home") { select(0, true); event.preventDefault(); }
      else if (event.key === "End") { select(n - 1, true); event.preventDefault(); }
    });
  });

  // A link carrying data-tab reveals that panel's tab before the anchor jumps to it.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-tab]");
    if (!link) return;
    var panel = document.getElementById(link.getAttribute("data-tab"));
    var tab = panel && document.getElementById(panel.getAttribute("aria-labelledby"));
    if (tab) tab.click();
  });
})();
