// Real-browser check of the first-run password, sign-out and sign-in forms (Chromium).
// Usage: ORAMA_TEST_CODE=<first-run code> ORAMA_TEST_PASSWORD=<password> \
//          node browser_signin.js <base-url>
// (the code and password go in the environment, never on the command line)
// Exit codes: 0 passed, 1 failed (reason on stderr), 3 no browser available.
"use strict";

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (e) {
  console.error("playwright is not installed");
  process.exit(3);
}

const base = process.argv[2];
const code = process.env.ORAMA_TEST_CODE;
const password = process.env.ORAMA_TEST_PASSWORD;

async function main() {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (e) {
    console.error("chromium cannot start: " + e.message);
    process.exit(3);
  }
  const refused = [];
  try {
    const page = await browser.newPage();
    page.on("response", (r) => {
      if (r.request().method() === "POST" && r.status() >= 400) {
        refused.push(`${r.status()} ${new URL(r.url()).pathname}`);
      }
    });
    const signOut = page.getByRole("button", { name: "Sign out" });

    // 1. The installer's link leads to the password form.
    await page.goto(`${base}/setup?code=${encodeURIComponent(code)}`);
    if (new URL(page.url()).pathname !== "/setup/password") {
      throw new Error("the setup link did not open the password page: " + page.url());
    }
    await page.fill('input[name="password"]', password);
    await page.getByRole("button", { name: "Create password" }).click();
    await signOut.waitFor({ timeout: 10000 });

    // 2. Sign out through the real form.
    await signOut.click();
    await page.waitForURL((u) => new URL(u).pathname === "/signin", { timeout: 10000 });

    // 3. Sign in again through the real form.
    await page.fill('input[name="password"]', password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await signOut.waitFor({ timeout: 10000 });
    if (new URL(page.url()).pathname === "/signin") {
      throw new Error("still on the sign-in page after signing in");
    }
    if (refused.length) {
      throw new Error("a form post was refused: " + refused.join(", "));
    }
    console.log("ok");
  } catch (e) {
    console.error(e.message + (refused.length ? ` (refused: ${refused.join(", ")})` : ""));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main();
