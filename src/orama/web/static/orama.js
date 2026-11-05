"use strict";

// CSRF is inherited through the body's JSON hx-headers (no evaluated expressions).
// Mark the current navigation link without adding route logic to shared templates.
document.querySelectorAll('nav[aria-label="Main navigation"] a').forEach((link) => {
  const path = new URL(link.href).pathname;
  if (window.location.pathname === path ||
      (path !== "/" && window.location.pathname.startsWith(path + "/"))) {
    link.setAttribute("aria-current", "page");
  }
});

// Setup wizard: show only the parts of a form that apply. Without this script every part
// stays visible and the server explains anything that does not fit.
(() => {
  const byId = (id) => document.getElementById(id);
  const sync = () => {
    document.querySelectorAll("[data-visible-if]").forEach((box) => {
      const control = byId(box.dataset.visibleIf);
      box.hidden = !(control && control.checked);
    });
    const provider = byId("vpn_provider");
    if (provider) {
      const option = provider.options[provider.selectedIndex];
      document.querySelectorAll("[data-feature]").forEach((box) => {
        const wanted = option && option.dataset[box.dataset.feature === "address" ? "needsAddress"
          : "portForwarding"] === "true";
        box.hidden = !wanted;
      });
    }
  };
  document.addEventListener("change", sync);
  sync();
})();
