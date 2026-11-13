(function () {
  var KEY = "coven-theme";
  function apply(t) { document.documentElement.setAttribute("data-theme", t); }
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) {}
  if (!saved) { saved = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; }
  apply(saved);
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) {}
    });
  });
})();

// Buttons marked data-print open the browser's print dialog (no inline handlers: the
// Content-Security-Policy allows scripts only from this site).
document.addEventListener("click", function (event) {
  if (event.target.closest && event.target.closest("[data-print]")) {
    window.print();
  }
});
