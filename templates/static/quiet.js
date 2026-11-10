// The quiet board's only script: a tooltip for chart marks and the light/dark switch.
// Everything it shows is also in each chart's <title> elements and table twin.
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
})();
