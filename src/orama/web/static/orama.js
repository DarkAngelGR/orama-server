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

// Password strength indicator for 'New password' field in settings
(() => {
  const initPasswordStrength = () => {
    const input = document.getElementById("new_password");
    if (!input) return;

    let container = document.getElementById("password-strength-meter");
    if (!container) {
      container = document.createElement("div");
      container.id = "password-strength-meter";
      container.className = "password-strength";
      container.setAttribute("aria-live", "polite");
      container.hidden = true;
      container.innerHTML =
        '<div class="strength-meter" role="meter" aria-label="Password strength" aria-valuemin="0" aria-valuemax="4" aria-valuenow="0" aria-valuetext="No password entered">' +
          '<span class="strength-segment"></span>' +
          '<span class="strength-segment"></span>' +
          '<span class="strength-segment"></span>' +
          '<span class="strength-segment"></span>' +
        '</div>' +
        '<div class="strength-label" id="password-strength-label"></div>';
      const parent = input.closest(".field") || input.parentNode;
      parent.appendChild(container);
    }

    const meter = container.querySelector(".strength-meter");
    const label = container.querySelector("#password-strength-label") || container.querySelector(".strength-label");

    const evaluate = (val) => {
      if (!val) return { score: 0, text: "", hint: "" };
      const len = val.length;
      if (len < 8) {
        return { score: 1, text: "Weak", hint: "Too short (at least 12 characters required)" };
      }
      let variety = 0;
      if (/[a-z]/.test(val)) variety++;
      if (/[A-Z]/.test(val)) variety++;
      if (/[0-9]/.test(val)) variety++;
      if (/[^a-zA-Z0-9]/.test(val)) variety++;

      const isUniform = new Set(val).size <= 2;

      if (len < 12) {
        if (isUniform || variety <= 1) {
          return { score: 1, text: "Weak", hint: "Too short (at least 12 characters required)" };
        }
        return { score: 2, text: "Fair", hint: "At least 12 characters required" };
      }

      if (isUniform || (variety === 1 && len < 16)) {
        return { score: 1, text: "Weak", hint: "Add uppercase, numbers, or symbols" };
      }

      if (variety <= 2) {
        if (len >= 16) {
          return { score: 3, text: "Good", hint: "Add symbols for maximum strength" };
        }
        return { score: 2, text: "Fair", hint: "Add uppercase, numbers, or symbols" };
      }

      if (variety === 3) {
        if (len >= 16) {
          return { score: 4, text: "Strong", hint: "Excellent password strength" };
        }
        return { score: 3, text: "Good", hint: "Add symbols for maximum strength" };
      }

      return { score: 4, text: "Strong", hint: "Excellent password strength" };
    };

    const update = () => {
      const val = input.value;
      if (!val) {
        container.hidden = true;
        container.removeAttribute("data-score");
        if (meter) {
          meter.setAttribute("aria-valuenow", "0");
          meter.setAttribute("aria-valuetext", "No password entered");
        }
        if (label) label.textContent = "";
        return;
      }

      container.hidden = false;
      const res = evaluate(val);
      container.setAttribute("data-score", String(res.score));
      if (meter) {
        meter.setAttribute("aria-valuenow", String(res.score));
        meter.setAttribute("aria-valuetext", res.text + (res.hint ? " — " + res.hint : ""));
      }
      if (label) {
        label.innerHTML = "<strong>" + res.text + "</strong>" +
          (res.hint ? ' <span class="strength-hint">· ' + res.hint + "</span>" : "");
      }
    };

    input.addEventListener("input", update);
    input.addEventListener("focus", update);
    if (input.value) update();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPasswordStrength);
  } else {
    initPasswordStrength();
  }
})();

