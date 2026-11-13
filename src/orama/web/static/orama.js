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

// Heuristic password strength guidance for 'New password' field in settings
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

      const meterEl = document.createElement("div");
      meterEl.className = "strength-meter";
      meterEl.setAttribute("aria-hidden", "true");
      for (let i = 0; i < 4; i++) {
        const seg = document.createElement("span");
        seg.className = "strength-segment";
        meterEl.appendChild(seg);
      }

      const labelEl = document.createElement("p");
      labelEl.className = "strength-label";
      labelEl.id = "password-strength-label";

      container.appendChild(meterEl);
      container.appendChild(labelEl);
      const parent = input.closest(".field") || input.parentNode;
      parent.appendChild(container);
    }

    const label =
      container.querySelector("#password-strength-label") ||
      container.querySelector(".strength-label");

    const evaluate = (val) => {
      if (!val) return { score: 0, text: "", hint: "" };
      const len = val.length;
      if (len < 8) {
        return {
          score: 1,
          text: "Weak",
          hint: "Too short (at least 12 characters required)",
        };
      }
      let variety = 0;
      if (/[a-z]/.test(val)) variety++;
      if (/[A-Z]/.test(val)) variety++;
      if (/[0-9]/.test(val)) variety++;
      if (/[^a-zA-Z0-9]/.test(val)) variety++;

      const isUniform = new Set(val).size <= 2;

      if (len < 12) {
        if (isUniform || variety <= 1) {
          return {
            score: 1,
            text: "Weak",
            hint: "Too short (at least 12 characters required)",
          };
        }
        return {
          score: 2,
          text: "Fair",
          hint: "At least 12 characters required",
        };
      }

      if (isUniform || (variety === 1 && len < 16)) {
        return {
          score: 1,
          text: "Weak",
          hint: "Heuristic guidance: add uppercase, numbers, or symbols, or use a longer passphrase",
        };
      }

      if (variety <= 2) {
        if (len >= 16) {
          return {
            score: 3,
            text: "Good",
            hint: "Heuristic guidance: add symbols or more words, and avoid common or reused passwords",
          };
        }
        return {
          score: 2,
          text: "Fair",
          hint: "Heuristic guidance: add uppercase, numbers, or symbols",
        };
      }

      if (variety === 3) {
        if (len >= 16) {
          return {
            score: 4,
            text: "Strong",
            hint: "Heuristic guidance only — choose a unique password not used elsewhere",
          };
        }
        return {
          score: 3,
          text: "Good",
          hint: "Heuristic guidance: add symbols or length, and avoid common passwords",
        };
      }

      return {
        score: 4,
        text: "Strong",
        hint: "Heuristic guidance only — choose a unique password not used elsewhere",
      };
    };

    const update = () => {
      const val = input.value;
      if (!val) {
        container.hidden = true;
        container.removeAttribute("data-score");
        if (label) label.textContent = "";
        return;
      }

      container.hidden = false;
      const res = evaluate(val);
      container.setAttribute("data-score", String(res.score));
      if (label) {
        label.textContent = "";
        const prefix = document.createElement("span");
        prefix.className = "strength-prefix";
        prefix.textContent = "Password guidance: ";
        const strong = document.createElement("strong");
        strong.textContent = res.text;
        label.appendChild(prefix);
        label.appendChild(strong);
        if (res.hint) {
          const hint = document.createElement("span");
          hint.className = "strength-hint";
          hint.textContent = " · " + res.hint;
          label.appendChild(hint);
        }
      }
    };

    input.addEventListener("input", update);
    if (input.value) update();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPasswordStrength);
  } else {
    initPasswordStrength();
  }
})();


// An app logo that cannot be fetched is removed, so the shipped icon or letter under it shows.
// Capture phase: "error" on an <img> does not bubble.
document.addEventListener("error", function (event) {
  var img = event.target;
  if (img && img.classList && img.classList.contains("app-logo")) img.remove();
}, true);
