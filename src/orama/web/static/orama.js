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
