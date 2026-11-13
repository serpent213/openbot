// Apply the saved theme before first paint so the page never flashes the wrong one. A file rather
// than an inline script, so the Content-Security-Policy can stay script-src 'self'.
try {
  var t = localStorage.getItem("openbot:theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch (e) {}
