// Real-browser pass over every Orama page (Chromium through Playwright).
// Usage: node browser_pass.js <scenario> <base-url>
//   scenario: wizard | keyboard | apps-focus
// The password, first-run code and every typed secret are made here or come in the
// environment, never on the command line. Environment (all optional but the first two):
//   ORAMA_TEST_CODE, ORAMA_TEST_PASSWORD, ORAMA_TEST_WIDTH (default 1280),
//   ORAMA_TEST_SCHEME (light|dark), ORAMA_TEST_SHOTS_DIR (save a PNG of every page),
//   ORAMA_TEST_AXE (path to axe-core's axe.min.js: also run its WCAG 2.1 AA rules)
// Exit codes: 0 passed, 1 failed (reasons on stderr), 3 no browser available.
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (e) {
  console.error("playwright is not installed");
  process.exit(3);
}

const scenario = process.argv[2];
const base = process.argv[3];
const code = process.env.ORAMA_TEST_CODE;
const password = process.env.ORAMA_TEST_PASSWORD;
const width = Number(process.env.ORAMA_TEST_WIDTH || 1280);
const scheme = process.env.ORAMA_TEST_SCHEME || "light";
const shotsDir = process.env.ORAMA_TEST_SHOTS_DIR || "";
const axePath = process.env.ORAMA_TEST_AXE || "";

// Secrets typed into forms: made here, and checked never to come back in any page.
const typed = {
  jellyfinPassword: "jf-" + crypto.randomBytes(12).toString("hex"),
  vpnKey: crypto.randomBytes(32).toString("base64"),
  indexerKey: "ix-" + crypto.randomBytes(12).toString("hex"),
  usenetPassword: "un-" + crypto.randomBytes(12).toString("hex"),
};

const problems = []; // everything the browser objected to, whatever page it was on
const allowed = new Set(); // "<status> <path>" answers a step expects on purpose
const shotNames = new Map();
let shotCount = 0;

function fail(message) {
  throw new Error(message);
}

function watch(page) {
  page.on("console", (m) => {
    // Chromium logs every refused form post; a step that expects one allows it by name.
    const refused = /Failed to load resource: the server responded with a status of (\d+)/.exec(m.text());
    if (refused && allowed.has(`${refused[1]} ${new URL(m.location().url || base).pathname}`)) return;
    if (m.type() === "error" || m.type() === "warning") {
      problems.push(`console ${m.type()} at ${m.location().url || "?"}: ${m.text()}`);
    }
  });
  page.on("pageerror", (e) => problems.push(`script error: ${e.message}`));
  page.on("requestfailed", (r) => {
    problems.push(`request failed: ${r.method()} ${new URL(r.url()).pathname} ${r.failure()?.errorText}`);
  });
  page.on("response", (r) => {
    const where = new URL(r.url());
    if (r.status() >= 400 && !allowed.has(`${r.status()} ${where.pathname}`)) {
      problems.push(`unexpected answer: ${r.status()} ${r.request().method()} ${where.pathname}`);
    }
  });
}

async function launch() {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (e) {
    console.error("chromium cannot start: " + e.message);
    process.exit(3);
  }
  const phone = width <= 480;
  const context = await browser.newContext({
    viewport: { width, height: phone ? 740 : 900 },
    colorScheme: scheme,
    isMobile: phone,
    hasTouch: phone,
    deviceScaleFactor: phone ? 2 : 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  watch(page);
  return { browser, page };
}

// ------------------------------------------------------------------ checks on every page

const INTERACTIVE = "a[href], button, input:not([type=hidden]), select, textarea, summary";

async function audit(page, label) {
  await page.waitForLoadState("load");
  const info = await page.evaluate((selector) => {
    const visible = (el) => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden";
    };
    const level = (h) => Number(h.tagName[1]);
    const headings = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")].filter(visible).map(level);
    const unnamed = [...document.querySelectorAll("input:not([type=hidden]),select,textarea")]
      .filter((el) => visible(el) || el.type === "checkbox" || el.type === "radio")
      .filter((el) => !(el.labels && el.labels.length) && !el.getAttribute("aria-label")
        && !el.getAttribute("aria-labelledby"))
      .map((el) => el.name || el.id);
    const brokenRefs = [...document.querySelectorAll("[aria-describedby],[aria-labelledby]")]
      .flatMap((el) => ((el.getAttribute("aria-describedby") || "") + " " +
        (el.getAttribute("aria-labelledby") || "")).split(/\s+/).filter(Boolean)
        .filter((id) => !document.getElementById(id)));
    const ids = {};
    document.querySelectorAll("[id]").forEach((el) => { ids[el.id] = (ids[el.id] || 0) + 1; });
    const duplicates = Object.keys(ids).filter((id) => ids[id] > 1);
    const small = [...document.querySelectorAll(selector)].filter(visible).filter((el) => {
      const r = el.getBoundingClientRect();
      return r.height < 44 || r.width < 44;
    }).map((el) => {
      const box = el.closest("label.choice, .wizard-step, li") || el;
      const r = box.getBoundingClientRect();
      const inline = el.matches("a") && !el.classList.contains("button") && !el.closest("nav")
        && !el.classList.contains("brand") && !el.classList.contains("skip-link");
      return { what: (el.textContent || el.name || el.id || el.tagName).trim().slice(0, 40),
               tag: el.tagName, w: Math.round(r.width), h: Math.round(r.height), inline };
    }).filter((x) => x.h < 44 || x.w < 44);
    return {
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      headings, unnamed, brokenRefs, duplicates, small,
      title: document.title, lang: document.documentElement.lang,
      html: document.documentElement.outerHTML,
      main: !!document.querySelector("main#content"),
    };
  }, INTERACTIVE);
  const where = `${label} (${new URL(page.url()).pathname})`;
  const errors = [];
  if (info.scrollWidth > width) errors.push(`sideways scrolling: page is ${info.scrollWidth}px wide in ${width}px`);
  if (info.headings.filter((h) => h === 1).length !== 1) errors.push(`needs exactly one h1, has ${info.headings.filter((h) => h === 1).length}`);
  if (info.headings.length && info.headings[0] !== 1) errors.push("the first heading is not the h1");
  info.headings.forEach((h, i) => {
    if (i && h > info.headings[i - 1] + 1) errors.push(`heading levels jump from h${info.headings[i - 1]} to h${h}`);
  });
  if (!info.title.trim() || !info.title.includes("Orama")) errors.push(`odd page title "${info.title}"`);
  if (info.lang !== "en") errors.push("html lang is not set");
  if (!info.main) errors.push("no main landmark");
  if (info.unnamed.length) errors.push(`controls without a label: ${info.unnamed.join(", ")}`);
  if (info.brokenRefs.length) errors.push(`aria references to missing ids: ${info.brokenRefs.join(", ")}`);
  if (info.duplicates.length) errors.push(`duplicate ids: ${info.duplicates.join(", ")}`);
  for (const [name, secret] of Object.entries(typed)) {
    if (info.html.includes(secret)) errors.push(`${name} came back in the page`);
  }
  if (info.html.includes(password || "\u0000")) errors.push("the Orama password came back in the page");
  // Buttons, form controls and nav links must be 44 px; inline links are only reported.
  const tooSmall = info.small.filter((x) => !x.inline && x.tag !== "INPUT");
  const smallInputs = info.small.filter((x) => x.tag === "INPUT" && x.h < 44);
  if (tooSmall.length || smallInputs.length) {
    errors.push("touch targets under 44px: " + [...tooSmall, ...smallInputs]
      .map((x) => `${x.what} ${x.w}x${x.h}`).join("; "));
  }
  if (axePath) {
    const axe = fs.readFileSync(axePath, "utf8");
    await page.evaluate(axe);
    const result = await page.evaluate(() => axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
    }));
    console.log(`note axe: ${result.passes.length} rules passed, ${result.violations.length} failed, ${result.incomplete.length} unsure on ${where}`);
    for (const v of result.incomplete) {
      console.log(`note axe unsure ${v.id}: ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
    }
    for (const v of result.violations) {
      errors.push(`axe ${v.id} (${v.impact}): ${v.help} — ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
    }
  }
  if (shotsDir) {
    fs.mkdirSync(shotsDir, { recursive: true });
    shotCount += 1;
    const name = `${scheme}-${width}-${String(shotCount).padStart(2, "0")}-${label.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.png`;
    shotNames.set(label, name);
    await page.screenshot({ path: path.join(shotsDir, name), fullPage: true });
  }
  if (errors.length) problems.push(`${where}: ${errors.join("; ")}`);
  const hints = info.small.filter((x) => x.inline).map((x) => `${x.what} ${x.w}x${x.h}`);
  if (hints.length) console.log(`note ${where}: small inline links: ${hints.join("; ")}`);
}

async function goto(page, urlPath, label) {
  await page.goto(base + urlPath);
  await audit(page, label);
}

async function click(page, locator, label) {
  // Click something that loads a new page, then check that page.
  const before = page.url();
  await Promise.all([page.waitForNavigation({ waitUntil: "load" }), locator.click()]);
  await audit(page, label);
  return before;
}

function pathOf(page) {
  return new URL(page.url()).pathname;
}

async function expectPath(page, expected) {
  if (pathOf(page) !== expected) fail(`expected ${expected} but the browser is on ${pathOf(page)}`);
}

// ------------------------------------------------------------------ the whole wizard

async function waitDone(page, what) {
  await page.locator("#job-status .notice-ok").waitFor({ timeout: 30000 });
  const text = await page.locator("#job-status").innerText();
  if (!text.includes("All done")) fail(`${what}: finished without "All done": ${text}`);
}

async function wizard() {
  const { browser, page } = await launch();
  try {
    // First run: the installer's link opens the password form; without it nothing opens.
    await goto(page, "/setup/password", "first run without a link");
    const bad = await page.request.get(`${base}/setup/password?code=wrong`, { failOnStatusCode: false });
    if (bad.status() !== 403) fail("a wrong first-run code was not refused");
    await page.goto(`${base}/setup?code=${encodeURIComponent(code)}`);
    await expectPath(page, "/setup/password");
    await audit(page, "create password");
    await page.getByLabel("Create your Orama password").fill(password);
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/media"),
      page.getByRole("button", { name: "Create password" }).click()]);
    await audit(page, "media servers");

    // Sign out and in again, the wrong password first.
    await click(page, page.getByRole("button", { name: "Sign out" }), "sign in");
    await expectPath(page, "/signin");
    allowed.add("401 /signin");
    await page.getByLabel("Orama password").fill(password + "x");
    await Promise.all([page.waitForNavigation(), page.getByRole("button", { name: "Sign in" }).click()]);
    await audit(page, "sign in, wrong password");
    const described = await page.getByLabel("Orama password").getAttribute("aria-describedby");
    if (!described || !(await page.locator("#" + described).innerText()).includes("not right")) {
      fail("the wrong-password message is not tied to the password field");
    }
    await page.getByLabel("Orama password").fill(password);
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/media"),
      page.getByRole("button", { name: "Sign in" }).click()]);
    await audit(page, "media servers");

    // 1. Media servers: Plex and Jellyfin (the Jellyfin fields appear when ticked).
    if (await page.getByLabel("Administrator name").isVisible()) fail("Jellyfin fields shown before Jellyfin is ticked");
    await page.getByLabel("Plex", { exact: true }).check();
    await page.getByLabel("Jellyfin", { exact: true }).check();
    await page.getByLabel("Administrator name").waitFor();
    await page.getByLabel("Administrator name").fill("owner");
    allowed.add("400 /setup/media");
    await page.getByLabel("Administrator password").fill("short");
    await click(page, page.getByRole("button", { name: "Save and continue" }), "media servers, wrong answer");
    await expectPath(page, "/setup/media");
    const jfError = page.locator("#jellyfin_password-error");
    if (!(await jfError.isVisible())) fail("a too-short Jellyfin password was not explained next to the field");
    if ((await page.getByLabel("Administrator name").inputValue()) !== "owner") fail("the other answers were lost");
    await page.getByLabel("Administrator password").fill(typed.jellyfinPassword);
    await click(page, page.getByRole("button", { name: "Save and continue" }), "folders");
    await expectPath(page, "/setup/folders");

    // 2. Folders: the installer's folder, shown and not editable.
    if (!(await page.getByText("It was chosen when Orama was installed.").isVisible())) fail("the media folder is not explained");
    if ((await page.locator("#data_root").count()) !== 0) fail("the media folder can be edited in the web app");
    await click(page, page.getByRole("button", { name: "Continue" }), "downloads and vpn");
    await expectPath(page, "/setup/downloads");

    // 3. Downloads: Usenet and torrents with a VPN. The first save starts the apps.
    await page.getByLabel("Torrents").check();
    await page.getByLabel("Send torrent downloads through a VPN").check();
    await page.getByLabel("WireGuard private key").waitFor();
    allowed.add("400 /setup/downloads");
    await page.getByLabel("WireGuard private key").fill("not a key");
    await click(page, page.getByRole("button", { name: "Save and start your apps" }), "downloads, wrong key");
    if (!(await page.locator("#vpn_key-error").isVisible())) fail("a wrong WireGuard key was not explained");
    await page.getByLabel("WireGuard private key").fill(typed.vpnKey);
    await click(page, page.getByRole("button", { name: "Save and start your apps" }), "starting your apps");
    await expectPath(page, "/setup/starting");
    await page.getByRole("link", { name: "Continue" }).waitFor({ timeout: 30000 });
    await audit(page, "starting your apps, done");
    await click(page, page.getByRole("link", { name: "Continue" }), "indexers");
    await expectPath(page, "/setup/indexers");

    // 4. Indexers: search, pick one, add and test; then a Usenet server.
    await page.getByLabel("Search indexers").fill("Generic");
    await click(page, page.getByRole("button", { name: "Search" }), "indexers, search");
    await click(page, page.getByRole("link", { name: /Generic/ }).first(), "indexers, add one");
    await page.getByLabel("API key").fill(typed.indexerKey);
    await page.getByLabel("Address", { exact: true }).fill("https://indexer.example");
    await click(page, page.getByRole("button", { name: "Add and test indexer" }), "indexers, tested");
    if (!(await page.locator(".notice-ok").innerText()).includes("works")) fail("the indexer test did not say it works");
    await page.getByLabel("Server address").fill("news.example");
    await page.getByLabel("Username").fill("reader");
    await page.getByLabel("Password", { exact: true }).fill(typed.usenetPassword);
    await click(page, page.getByRole("button", { name: "Add and test server" }), "indexers, usenet tested");
    if (!(await page.locator(".notice-ok").innerText()).includes("works")) fail("the Usenet test did not say it works");
    await click(page, page.getByRole("link", { name: "Continue" }), "playback");
    await expectPath(page, "/setup/playback");

    // 5. Playback and subtitles.
    await page.getByLabel("LG TV").check();
    await page.getByLabel("1080p — smaller files").check();
    await click(page, page.getByRole("button", { name: "Save and continue" }), "subtitles");
    await page.getByLabel("First choice").selectOption({ label: "English" });
    await page.getByLabel("Second choice").selectOption({ label: "Greek" });
    await click(page, page.getByRole("button", { name: "Save and continue" }), "review");
    await expectPath(page, "/setup/review");

    // 6. Review and Install: the checklist grows on its own and its poller stops.
    const polls = [];
    page.on("request", (r) => {
      if (["xhr", "fetch"].includes(r.resourceType())) polls.push(new URL(r.url()).pathname);
    });
    await page.getByRole("button", { name: "Install" }).waitFor();
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/finish"),
      page.getByRole("button", { name: "Install" }).click()]);
    await page.waitForLoadState("load");
    await page.evaluate(() => { window.__same_page = true; });
    await page.evaluate(() => {
      window.__added = [];
      new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => {
        if (n.nodeType !== 1) return;
        const items = n.tagName === "LI" ? [n] : [...n.querySelectorAll("li")];
        items.forEach((li) => window.__added.push(`LI:${li.textContent.trim()}`));
      }))).observe(document.querySelector(".checklist"), { childList: true, subtree: true });
    });
    const seen = new Set();
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      seen.add(await page.locator("#checklist-items").innerText());
      if (await page.locator("#job-status .notice-ok").count()) break;
      await page.waitForTimeout(250);
    }
    await waitDone(page, "install");
    seen.add(await page.locator("#checklist-items").innerText());
    if (seen.size < 3) fail(`the checklist did not update on its own (saw ${seen.size} different lists)`);
    if (!(await page.evaluate(() => window.__same_page === true))) fail("the finish page reloaded instead of updating");
    // The live region gets each line once: no whole list is put back in on every poll.
    const added = (await page.evaluate(() => window.__added)).filter((x) => x.startsWith("LI:"));
    if (new Set(added).size !== added.length) fail(`the checklist re-sent lines already shown: ${added.join(" | ")}`);
    if (added.length < 3) fail(`only ${added.length} lines were added: ${added.join(" | ")}`);
    await audit(page, "finish");
    await page.getByRole("button", { name: "Sign in with Plex" }).waitFor();
    if ((await page.locator("#job-poller").getAttribute("hx-get")) !== null) fail("the status poller is still armed after the job ended");
    const countNow = polls.length;
    await page.waitForTimeout(5000);
    const stray = polls.slice(countNow);
    if (stray.length) fail(`polling went on after the job ended: ${stray.join(", ")}`);
    if (!(await page.locator("#finish-actions").innerText()).includes("Jellyfin is ready")) fail("the finish page does not say Jellyfin is ready");

    // 7. Plex sign-in: a code, then the approval arrives and the page moves on by itself.
    await click(page, page.getByRole("button", { name: "Sign in with Plex" }), "plex sign-in");
    await expectPath(page, "/plex/sign-in/status");
    if (!(await page.locator("strong").innerText()).match(/^[A-Za-z0-9]{4}$/)) fail("no Plex code shown");
    await page.waitForURL((u) => new URL(u).pathname === "/setup/finish", { timeout: 30000 });
    await waitDone(page, "plex");
    await audit(page, "finish, plex connected");
    if (!(await page.locator("#finish-actions").innerText()).includes("Plex sign-in is saved")) fail("Plex sign-in not saved");

    // 8. Settings: save a section (an apply runs), then the VPN-off confirmation.
    await click(page, page.getByRole("link", { name: "Settings" }), "settings");
    await page.locator("#playback").getByLabel("Apple TV").check();
    await click(page, page.locator("#playback").getByRole("button", { name: "Save" }), "settings, saved");
    await expectPath(page, "/settings");
    await waitDone(page, "settings save");
    await audit(page, "settings, applied");
    await page.locator("#downloads").getByLabel("Send torrent downloads through a VPN").uncheck();
    await click(page, page.locator("#downloads").getByRole("button", { name: "Save" }), "confirm turning the vpn off");
    if (!(await page.locator(".notice").first().innerText()).includes("VPN off")) fail("no VPN-off confirmation");
    await click(page, page.getByRole("button", { name: "Yes, turn it off" }), "settings, vpn off applied");
    await waitDone(page, "vpn off");
    await click(page, page.getByRole("link", { name: "Manage indexers" }), "settings, indexers");
    await goto(page, "/settings#addresses", "settings, addresses");

    // 9. Apps and Home.
    await click(page, page.getByRole("link", { name: "Apps" }), "apps");
    await page.locator("#app-open-plex").waitFor();
    const rows = await page.locator(".app-row").count();
    if (rows < 5) fail(`the Apps page lists only ${rows} apps`);
    await click(page, page.getByRole("link", { name: "Home", exact: true }), "home");
    allowed.add("404 /not-a-page");
    await goto(page, "/not-a-page", "page that does not exist");
    await click(page, page.getByRole("link", { name: "Orama home" }), "home again");

    // Sign out leaves the sign-in page and nothing behind.
    await click(page, page.getByRole("button", { name: "Sign out" }), "signed out");
    await goto(page, "/apps", "apps while signed out");
    await expectPath(page, "/signin");
  } finally {
    await browser.close();
  }
}

// ------------------------------------------------------------------ keyboard only

async function focusVisible(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el || el === document.body) return "nothing has focus";
    const s = getComputedStyle(el);
    const ring = s.outlineStyle !== "none" && parseFloat(s.outlineWidth) >= 2;
    return ring ? "" : `no visible focus ring on ${el.tagName} ${el.id || el.name || ""}`;
  });
}

async function tabTo(page, selector, limit = 60) {
  for (let i = 0; i < limit; i += 1) {
    await page.keyboard.press("Tab");
    const problem = await focusVisible(page);
    if (problem) fail(`${problem} (looking for ${selector} after ${i + 1} Tab presses on ${pathOf(page)})`);
    if (await page.evaluate((s) => document.activeElement.matches(s), selector)) return;
  }
  fail(`Tab never reached ${selector}`);
}

async function keyboard() {
  const { browser, page } = await launch();
  try {
    await page.goto(`${base}/signin`);
    // The first Tab stop is the skip link; Enter on it moves focus to the content.
    await page.keyboard.press("Tab");
    if (!(await page.evaluate(() => document.activeElement.classList.contains("skip-link")))) fail("the first Tab stop is not the skip link");
    await page.keyboard.press("Enter");
    if (!(await page.evaluate(() => document.activeElement.id === "content"))) fail("the skip link did not move focus to the content");
    await tabTo(page, "#password");
    await page.keyboard.type(password);
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/media"), page.keyboard.press("Enter")]);
    await audit(page, "media servers (keyboard)");

    // The step list opens with Enter and is a real list of links.
    await tabTo(page, "summary");
    await page.keyboard.press("Enter");
    if (!(await page.evaluate(() => document.querySelector("details").open))) fail("Enter did not open the step list");
    await page.keyboard.press("Enter");

    await tabTo(page, "#server_plex");
    await page.keyboard.press("Space");
    await tabTo(page, "#server_jellyfin");
    await page.keyboard.press("Space");
    await tabTo(page, "#jellyfin_user");
    await page.keyboard.type("owner");
    await tabTo(page, "#jellyfin_password");
    await page.keyboard.type(typed.jellyfinPassword);
    await tabTo(page, "button[type=submit]");
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/folders"), page.keyboard.press("Enter")]);
    await audit(page, "folders (keyboard)");

    await tabTo(page, "form.wizard-form button[type=submit]");  // not the header's Sign out
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/downloads"), page.keyboard.press("Enter")]);
    await audit(page, "downloads (keyboard)");

    await tabTo(page, "#torrent");
    await page.keyboard.press("Space");
    await tabTo(page, "#vpn");
    await page.keyboard.press("Space");
    await tabTo(page, "#vpn_provider");
    await tabTo(page, "#vpn_key");
    await page.keyboard.type(typed.vpnKey);
    await tabTo(page, "button[type=submit]");
    await Promise.all([page.waitForURL((u) => new URL(u).pathname === "/setup/starting"), page.keyboard.press("Enter")]);
    await audit(page, "starting (keyboard)");
    await page.getByRole("link", { name: "Continue" }).waitFor({ timeout: 30000 });
    await tabTo(page, "a.button");
    const label = await page.evaluate(() => document.activeElement.textContent.trim());
    if (label !== "Continue") fail(`expected the Continue link to be reachable, found ${label}`);
  } finally {
    await browser.close();
  }
}

// ------------------------------------------------------------------ the Apps page

async function appsFocus() {
  const { browser, page } = await launch();
  try {
    await page.goto(`${base}/signin`);
    await page.getByLabel("Orama password").fill(password);
    await Promise.all([page.waitForNavigation(), page.getByRole("button", { name: "Sign in" }).click()]);
    await goto(page, "/apps", "apps");
    await page.locator("#app-open-plex").focus();
    const refresh = page.waitForResponse((r) => new URL(r.url()).pathname === "/apps/list", { timeout: 20000 });
    await refresh;
    await page.waitForTimeout(300); // let htmx finish the swap
    const id = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if (id !== "app-open-plex") fail(`the refresh took keyboard focus away (focus is on "${id}")`);
    // The same row stays the same element across a second refresh.
    await page.waitForResponse((r) => new URL(r.url()).pathname === "/apps/list", { timeout: 20000 });
    await page.waitForTimeout(300);
    if ((await page.evaluate(() => document.activeElement.id)) !== "app-open-plex") fail("the second refresh took focus away");
  } finally {
    await browser.close();
  }
}

// ------------------------------------------------------------------ main

async function main() {
  const scenarios = { wizard, keyboard, "apps-focus": appsFocus };
  if (!scenarios[scenario]) {
    console.error("unknown scenario " + scenario);
    process.exit(1);
  }
  try {
    await scenarios[scenario]();
  } catch (e) {
    problems.unshift(e.message);
  }
  if (problems.length) {
    console.error([...new Set(problems)].join("\n"));
    process.exitCode = 1;
    return;
  }
  console.log("ok");
}

main();
