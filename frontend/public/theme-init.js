// External script keeps CSP script-src 'self'; apply before first paint.
try {
  var t = localStorage.getItem("openbot:theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch { /* Storage can be disabled; use the OS theme. */ }
(function () {
  var system = window.matchMedia("(prefers-color-scheme: dark)");
  function updateChrome() {
    var preference = document.documentElement.getAttribute("data-theme");
    var dark = preference === "dark" || (preference !== "light" && system.matches);
    // Both media variants agree with an explicit preference; system mode follows the OS.
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (meta) {
      meta.setAttribute("content", dark ? "#0e1426" : "#f6f4ef");
    });
  }
  updateChrome();
  system.addEventListener("change", updateChrome);
  new MutationObserver(updateChrome).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
