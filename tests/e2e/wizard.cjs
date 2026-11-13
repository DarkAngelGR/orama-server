// Real-browser wizard + request flow for Plan 5 Task 3 E2E proofs.
// Usage:
//   node wizard.cjs <setup-url> <answers.json>
//   node wizard.cjs --verify-ready <verify.json>
// Reuses createSession, audit, click, goto, expectPath from ../web/browser_pass.cjs.
"use strict";

const fs = require("fs");
const { createSession, pathOf, fail } = require("../web/browser_pass.cjs");

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

async function waitForJobDone(page, what, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const errCount = await page.locator("#job-status .notice-problem, #job-status .notice-error").count();
    if (errCount > 0) {
      const text = await page.locator("#job-status").innerText();
      const lines = await page.locator("#checklist-items").innerText().catch(() => "");
      fail(`${what} reported an error: ${text}\nChecklist:\n${lines}`);
    }
    const okCount = await page.locator("#job-status .notice-ok").count();
    if (okCount > 0) {
      const pollerGet = await page.locator("#job-poller").getAttribute("hx-get");
      const finishGet = (await page.locator("#finish-actions[hx-get]").count()) > 0;
      if (pollerGet === null && !finishGet) {
        const text = await page.locator("#job-status").innerText();
        if (!text.includes("All done")) {
          fail(`${what} finished without "All done": ${text}`);
        }
        return;
      }
    }
    await page.waitForTimeout(1000);
  }
  const lastStatus = await page.locator("#job-status").innerText().catch(() => "");
  const lastChecklist = await page.locator("#checklist-items").innerText().catch(() => "");
  fail(`${what} timed out after ${Math.round(timeoutMs / 1000)}s.\nStatus: ${lastStatus}\nChecklist:\n${lastChecklist}`);
}

async function waitForIndexerSearch(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const hasSearch = (await page.locator('input[name="q"]').count()) > 0;
    const stillPolling = (await page.locator("#indexer-catalogue[hx-get]").count()) > 0;
    if (hasSearch && !stillPolling) {
      return;
    }
    await page.waitForTimeout(1000);
  }
  const text = await page.locator("#indexer-catalogue").innerText().catch(() => "");
  fail(`Prowlarr schema search did not become ready within ${Math.round(timeoutMs / 1000)}s: ${text}`);
}

async function waitSafeToLeaveRequest(page) {
  const hasPoller = (await page.locator("section[hx-get]").count()) > 0;
  if (hasPoller) {
    // Wait until a /status poll finishes so navigation occurs in the quiet 5s interval.
    await page
      .waitForResponse((r) => new URL(r.url()).pathname.endsWith("/status"), { timeout: 8000 })
      .catch(() => {});
    await page.waitForTimeout(100);
  }
}

async function readRequestState(page) {
  return page.evaluate(() => {
    const stageEl = document.querySelector(".app-state");
    const main = document.querySelector("main#content");
    return {
      stageText: stageEl ? stageEl.textContent.trim() : "",
      bodyText: main ? main.innerText : document.body.innerText,
      live: Boolean(document.querySelector("section[hx-get]")),
    };
  });
}

function isRequestFullyReady(state, deviceNote, primarySub) {
  const stage = (state.stageText || "").trim().toLowerCase();
  const body = (state.bodyText || "").toLowerCase();
  return (
    stage === "ready" &&
    body.includes(`direct play on ${String(deviceNote).toLowerCase()}`) &&
    !body.includes("still looking") &&
    body.includes(`${String(primarySub).toLowerCase()} subs`)
  );
}

async function createMediaRequest(page, spec, audit, click, expectPath) {
  await page.getByLabel("Title").fill(spec.query);
  const typeLabel = spec.kind === "movie" ? "Movie" : "TV show";
  await page.getByLabel(typeLabel, { exact: true }).check();
  await click(page, page.getByRole("button", { name: "Search" }), `search ${spec.query}`);
  await expectPath(page, "/search");

  const mediaType = spec.kind === "movie" ? "movie" : "series";
  const detailLink = page.locator(`a[href="/media/${mediaType}/${spec.source_id}"]`).first();
  if ((await detailLink.count()) === 0) {
    const pageText = await page.locator("main#content").innerText();
    fail(`Search for "${spec.query}" did not include /media/${mediaType}/${spec.source_id}.\n${pageText}`);
  }
  await click(page, detailLink, `media ${spec.query}`);
  await expectPath(page, `/media/${mediaType}/${spec.source_id}`);

  if (spec.kind === "movie") {
    await click(page, page.getByRole("button", { name: "Request movie" }), `requested ${spec.query}`);
  } else if (spec.kind === "episode") {
    await page.locator("#ep-season").fill(String(spec.season));
    await page.locator("#ep-number").fill(String(spec.episode));
    await click(page, page.getByRole("button", { name: "Request episode" }), `requested ${spec.query}`);
  } else {
    fail(`Unsupported request kind in wizard.cjs: ${spec.kind}`);
  }
  const reqPath = pathOf(page);
  if (!reqPath.startsWith("/requests/")) {
    fail(`Expected redirect to /requests/<id>, landed on ${reqPath}`);
  }
  const reqUrl = page.url();
  const state = await readRequestState(page);
  return {
    title: spec.query,
    kind: spec.kind,
    url: reqUrl,
    ...state,
  };
}

async function runSetupAndRequests(setupUrlOrBase, answersPath) {
  const answers = readJson(answersPath);
  const parsedArg = new URL(setupUrlOrBase);
  const baseUrl = parsedArg.origin;
  const setupCode = parsedArg.searchParams.get("code") || answers.setup_code || "";
  const setupUrl = parsedArg.pathname.startsWith("/setup")
    ? setupUrlOrBase
    : `${baseUrl}/setup?code=${encodeURIComponent(setupCode)}`;

  const session = createSession({
    baseUrl,
    password: answers.orama_password,
    extraSecrets: {
      jellyfinPassword: answers.jellyfin_password,
      indexerKey: answers.indexer_key,
    },
  });
  const { launch, audit, goto, click, expectPath, problems, extraSecrets } = session;
  const { browser, page } = await launch();

  try {
    // 1. Open setup URL -> create Orama password
    await page.goto(setupUrl, { waitUntil: "load" });
    await expectPath(page, "/setup/password");
    await audit(page, "create password");
    if (setupCode) {
      extraSecrets.setupCode = setupCode;
    }
    await page.getByLabel("Create your Orama password").fill(answers.orama_password);
    await Promise.all([
      page.waitForURL((u) => new URL(u).pathname === "/setup/media"),
      page.getByRole("button", { name: "Create password" }).click(),
    ]);
    await audit(page, "media servers");

    // 2. Media servers: Jellyfin only
    const plexBox = page.getByLabel("Plex", { exact: true });
    if (await plexBox.isChecked()) await plexBox.uncheck();
    await page.getByLabel("Jellyfin", { exact: true }).check();
    await page.getByLabel("Administrator name").waitFor();
    await page.getByLabel("Administrator name").fill(answers.jellyfin_user);
    await page.getByLabel("Administrator password").fill(answers.jellyfin_password);
    await click(page, page.getByRole("button", { name: "Save and continue" }), "folders");
    await expectPath(page, "/setup/folders");

    // 3. Folders
    await click(page, page.getByRole("button", { name: "Continue" }), "downloads");
    await expectPath(page, "/setup/downloads");

    // 4. Downloads: torrents only, no VPN
    const usenetBox = page.getByLabel("Usenet", { exact: true });
    if (await usenetBox.isChecked()) await usenetBox.uncheck();
    await page.getByLabel("Torrents", { exact: true }).check();
    const vpnBox = page.getByLabel("Send torrent downloads through a VPN");
    if (await vpnBox.isChecked()) await vpnBox.uncheck();
    await click(page, page.getByRole("button", { name: "Save and start your apps" }), "starting apps");
    await expectPath(page, "/setup/starting");

    await waitForJobDone(page, "starting apps", 900000);
    await audit(page, "starting apps done");
    await click(page, page.getByRole("link", { name: "Continue" }), "indexers");
    await expectPath(page, "/setup/indexers");

    // 5. Indexers: wait for Prowlarr schema, search Generic Torznab, add & test fixture indexer
    await waitForIndexerSearch(page, 240000);
    await audit(page, "indexers ready");
    await page.getByLabel("Search indexers").fill(answers.indexer_search || "Generic");
    await click(page, page.getByRole("button", { name: "Search" }), "indexers search");

    const torznabLink = page.locator(".indexer-results a").filter({ hasText: /Generic\s+Torznab/i }).first();
    const fallbackLink = page.locator(".indexer-results a").filter({ hasText: /Generic/i }).first();
    const picked = (await torznabLink.count()) > 0 ? torznabLink : fallbackLink;
    if ((await picked.count()) === 0) {
      fail("No Generic Torznab entry found in Prowlarr indexer search results");
    }
    await click(page, picked, "indexer form");

    await page.locator('input[name="name"]').fill(answers.indexer_name);
    await page.locator('input[name="field:baseUrl"]').fill(answers.indexer_url);
    if ((await page.locator('input[name="field:apiPath"]').count()) > 0) {
      await page.locator('input[name="field:apiPath"]').fill(answers.indexer_api_path || "/api");
    }
    await page.locator('input[name="field:apiKey"]').fill(answers.indexer_key);

    await Promise.all([
      page.waitForNavigation({ waitUntil: "load", timeout: 90000 }),
      page.getByRole("button", { name: "Add and test indexer" }).click(),
    ]);
    await audit(page, "indexer tested");
    const noticeText = await page.locator(".notice-ok").innerText().catch(() => "");
    if (!noticeText.includes("works")) {
      const errNotice = await page.locator(".notice-problem").innerText().catch(() => "");
      fail(`Indexer test did not pass: ${noticeText || errNotice}`);
    }
    await click(page, page.getByRole("link", { name: "Continue" }), "playback");
    await expectPath(page, "/setup/playback");

    // 6. Playback: Samsung S95F + 1080p
    await page.getByLabel(answers.device, { exact: true }).check();
    await page.getByLabel(answers.quality, { exact: true }).check();
    await click(page, page.getByRole("button", { name: "Save and continue" }), "subtitles");
    await expectPath(page, "/setup/subtitles");

    // 7. Subtitles: Greek then English
    await page.getByLabel("First choice").selectOption({ label: answers.subtitle_languages[0] });
    await page.getByLabel("Second choice").selectOption({ label: answers.subtitle_languages[1] });
    await click(page, page.getByRole("button", { name: "Save and continue" }), "review");
    await expectPath(page, "/setup/review");

    // 8. Review & Install
    await Promise.all([
      page.waitForURL((u) => new URL(u).pathname === "/setup/finish"),
      page.getByRole("button", { name: "Install" }).click(),
    ]);
    await waitForJobDone(page, "install finish", 900000);
    await audit(page, "finish done");

    const checklistLines = await page.locator("#checklist-items li").allInnerTexts();
    if (!checklistLines.length || checklistLines.some((line) => line.trimStart().startsWith("✗"))) {
      fail(`Finish checklist failed or empty: ${checklistLines.join(" | ")}`);
    }
    const finishActionsText = await page.locator("#finish-actions").innerText();
    if (!finishActionsText.includes("Jellyfin is ready")) {
      fail(`Finish actions missing Jellyfin ready message: ${finishActionsText}`);
    }

    // 9. Navigate Home and submit each request
    const trackedRequests = [];
    for (const reqSpec of answers.requests) {
      await waitSafeToLeaveRequest(page);
      await goto(page, "/", `home before ${reqSpec.query}`);
      await expectPath(page, "/");
      const created = await createMediaRequest(page, reqSpec, audit, click, expectPath);
      trackedRequests.push(created);
    }

    // 10. Poll each request page until Ready with direct play & subtitle confirmation
    const readyDeadline = Date.now() + 1200000; // 20 min max
    let activeIdx = trackedRequests.length - 1;
    while (Date.now() < readyDeadline) {
      let allDone = true;
      for (let i = 0; i < trackedRequests.length; i += 1) {
        const current = trackedRequests[i];
        if (isRequestFullyReady(current, answers.device_note, answers.primary_sub)) {
          continue;
        }
        allDone = false;
        if (activeIdx !== i || !current.live) {
          await waitSafeToLeaveRequest(page);
          await page.goto(current.url, { waitUntil: "load" });
          activeIdx = i;
        } else {
          // Stay on active live page briefly so htmx poll updates without reloading mid-flight
          await page
            .waitForResponse((r) => new URL(r.url()).pathname.endsWith("/status"), { timeout: 7000 })
            .catch(() => {});
          await page.waitForTimeout(200);
        }
        const refreshed = await readRequestState(page);
        current.stageText = refreshed.stageText;
        current.bodyText = refreshed.bodyText;
        current.live = refreshed.live;

        if (current.stageText.trim().toLowerCase() === "unavailable") {
          fail(`Request "${current.title}" entered Unavailable:\n${current.bodyText}`);
        }
      }
      if (allDone) break;
      await page.waitForTimeout(5000);
    }

    for (let i = 0; i < trackedRequests.length; i += 1) {
      const current = trackedRequests[i];
      if (!isRequestFullyReady(current, answers.device_note, answers.primary_sub)) {
        fail(
          `Request "${current.title}" did not reach Ready + ${answers.device_note} + ${answers.primary_sub} subs within timeout.\nStage: ${current.stageText}\nBody:\n${current.bodyText}`
        );
      }
      await waitSafeToLeaveRequest(page);
      await page.goto(current.url, { waitUntil: "load" });
      await audit(page, `ready ${current.title}`);
      const finalState = await readRequestState(page);
      current.stageText = finalState.stageText;
      current.bodyText = finalState.bodyText;
    }

    // A fresh visitor can acquire a valid CSRF token, but the spent code cannot
    // create an administrator or start a session again.
    const visitor = await browser.newContext();
    try {
      const signIn = await visitor.request.get(`${baseUrl}/signin`);
      const csrf = (await signIn.text()).match(/name="csrf_token"\s+value="([^"]+)"/);
      if (!signIn.ok() || !csrf) fail("Could not obtain a fresh sign-in CSRF token");
      const replay = await visitor.request.post(`${baseUrl}/setup/password`, {
        form: { csrf_token: csrf[1], code: setupCode, password: answers.orama_password },
        headers: { Origin: baseUrl },
        maxRedirects: 0,
      });
      if (replay.status() !== 303 || replay.headers().location !== "/signin") {
        fail("Replaying the old setup code did not return the visitor to sign-in");
      }
      const replayBody = await replay.text();
      if (replayBody.includes(setupCode) || replayBody.includes(answers.orama_password)) {
        fail("Setup replay response contained a secret");
      }
    } finally {
      await visitor.close();
    }

    if (problems.length) {
      fail([...new Set(problems)].join("\n"));
    }

    console.log(
      JSON.stringify({
        checklistOk: true,
        checklistLines: checklistLines.map((s) => s.trim()),
        finishActionsText: finishActionsText.trim(),
        requests: trackedRequests.map((r) => ({
          title: r.title,
          kind: r.kind,
          url: r.url,
          stageText: r.stageText,
          bodyText: r.bodyText,
        })),
      })
    );
  } finally {
    await browser.close();
  }
}

async function verifyReady(verifyPath) {
  const cfg = readJson(verifyPath);
  const session = createSession({
    baseUrl: cfg.base_url,
    password: cfg.orama_password,
  });
  const { launch, audit, expectPath, problems } = session;
  const { browser, page } = await launch();

  try {
    await page.goto(`${cfg.base_url}/signin`, { waitUntil: "load" });
    await audit(page, "signin post-reinstall");
    await page.getByLabel("Orama password").fill(cfg.orama_password);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "load" }),
      page.getByRole("button", { name: "Sign in" }).click(),
    ]);
    await expectPath(page, "/");

    const results = [];
    for (const url of cfg.request_urls) {
      await page.goto(url, { waitUntil: "load" });
      await audit(page, `verify ready ${new URL(url).pathname}`);
      const state = await readRequestState(page);
      const title = await page.locator("main#content h1").innerText();
      results.push({
        url,
        title: title.trim(),
        stageText: state.stageText,
        bodyText: state.bodyText,
      });
    }

    if (problems.length) {
      fail([...new Set(problems)].join("\n"));
    }

    console.log(JSON.stringify({ requests: results }));
  } finally {
    await browser.close();
  }
}

async function main() {
  const arg1 = process.argv[2];
  const arg2 = process.argv[3];
  if (!arg1 || !arg2) {
    console.error("Usage: node wizard.cjs <setup-url> <answers.json> | --verify-ready <verify.json>");
    process.exit(1);
  }
  try {
    if (arg1 === "--verify-ready") {
      await verifyReady(arg2);
    } else {
      await runSetupAndRequests(arg1, arg2);
    }
  } catch (e) {
    console.error(e && e.stack ? e.stack : String(e));
    process.exit(1);
  }
}

main();
