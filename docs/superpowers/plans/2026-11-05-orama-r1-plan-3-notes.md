# Plan 3 notes: the web app

How the web app was actually built. Where this differs from the plan text, these notes win.

## Decisions taken in Plan 3

- Flask app, server-rendered pages, vendored htmx 2.0.11 (hash-pinned), no build step, no CDN.
- One admin and one password (argon2id). The code separates "who is signed in" from "what they may do".
- The installer leaves a draft (`setup.draft.json`). The wizard edits the draft. The draft becomes the
  real `setup.json` at the first apply (the Downloads step) or at Install, and the draft is deleted.
- Settings edits `setup.json` directly, and only while no draft exists. With a draft it offers
  "Continue it" or "Throw it away" and saves nothing.
- Indexer and Usenet changes in Settings are tested first and saved only when the test passes. If a
  passed test is not saved, the app is put back (the old entry is tested again, or a removal marker
  is written for the next apply).
- Turning an app or the VPN off asks first (a confirm page). Secrets are never put in that page: the
  owner types them again or leaves them empty to keep the saved one.
- Secrets typed into forms are never shown again. An empty secret box keeps the stored value.
- Polling pages (checklist, starting, Apps) swap only what changed. The checklist appends new lines
  only. The Apps list swaps children, so keyboard focus survives a refresh.
- Plex sign-in: the PIN lives on the server; checking it is a CSRF-protected POST.
- A real browser now tests the whole app (`tests/web/test_browser.py`, `browser_pass.js`), and runs in a
  separate CI job.

## Security decisions worth knowing

- **Sessions are bound to the address they started from.** Browsers send cookies to every port on a
  host, and an app container on `orama-net` could replay a stolen cookie. A replay from another address
  ends the owner's session. This includes the pre-sign-in session.
- **`Referrer-Policy: same-origin`**, not `no-referrer`. A real browser sends `Origin: null` on form
  posts when the policy is `no-referrer`, and the CSRF check would then refuse every sign-in. Nothing
  leaves the site with this policy. (Older plan text says `no-referrer`; leave it.)
- **First-run code.** Made by `bootstrap` only while no password exists, printed in the installer's
  link, deleted when the password is created. A wrong code counts as a failed try.
- **Rate limits.** 5 failures in 15 minutes per address, 30 in 15 minutes for everyone together.
- **Host allowlist.** Only the machine's own addresses and names are answered (Settings → Addresses
  adds more). Anything else gets a plain page with no names in it.
- **HTTPS behind a proxy is not supported in R1.** The Origin check expects `http://<Host>`.
- **Pre-sign-in session flood is accepted.** At most 1000 are kept. Any container on `orama-net`
  could already trip the global login limiter, so this adds no new reach.
- **To verify on the NAS (Plan 5):** Docker's userland proxy may show the bridge gateway as the source
  address of connections made from the host itself. If it does, "bound to address" still works but
  every local visitor looks the same.
- A WireGuard key must look like one (44 characters, ends with `=`), on top of the allowed-characters check.
- Nothing in the candidate-test path logs what the owner typed (checked by reading the code and by a test).

## Design and accessibility review (Task 12)

Done with `design:ux-copy`, `design:accessibility-review` and `design:design-critique` on the real pages and
on screenshots at 360 and 1280 px, light and dark. Also axe-core (WCAG 2.1 A and AA rules, including
colour contrast) on every page in both colour schemes: zero violations.

Fixed:
- The checklist was re-sent as a whole list every 2 s, so a screen reader would read every line again each
  time. Now only new lines are appended; an unchanged poll sends nothing.
- The "Applying your setup" notice, the Plex "Waiting" text and the "Prowlarr is still starting" text were live
  regions that were replaced on every poll. They are plain text now; the final state is announced once.
- Embedded Settings groups use `h3` under the section `h2` (the outline was flat).
- "Back to installation", "Back to Settings" and "Refresh installation status" were 20 px tall; now 44 px.
- Page titles (`h1`) were up to 64 px on working pages; now at most 40 px, with spacing under the Apps title.
- The finish page's "Apply again" touched the notice and competed with "Sign in with Plex"; it now has space
  and is a secondary button once something has run.
- Links styled as buttons (`<a class="button">`) were inline and ignored their 48 px height: 43 px on the CI
  runner's fonts. Found by the browser job; `.button` is now `inline-flex`.
- The phone header: tighter spacing, one line where it fits.
- Wizard indexer and Usenet entries look like the cards used in Settings.
- Wording: "Use at least 12 characters (no more than 256)." instead of "between 12 and 256 characters long";
  the WireGuard error says what a key looks like; the confirm button says "Yes, turn it off".
- The step list marked Indexers "Done" by position. It now says "Needs a test" until an indexer (and, with
  Usenet, a server) has passed a test.
- The "Orama does not move files" note on the Folders step shows only after apps were really applied.

ACCEPTED:
- **Settings is one long page** (five forms, about 6000 px on a phone). Each section has its own Save and an
  id; splitting it into tabs is a design change for later. Reason: nothing is hidden from keyboard or
  screen-reader users, and a search-first Home arrives in Plan 4.
- **Home is a placeholder** ("Requests arrive in the next update"). Plan 4 replaces it.
- **After a rejected form, focus stays at the top of the new page.** The error notice is `role="alert"` and
  each wrong field is marked `aria-invalid` and tied to its message with `aria-describedby`, so it is
  announced. Moving focus to the first error would be an extra.
- **"Sign out" may sit on a second row in the header at exactly 360 px** (four items, long words). It stays a
  44 px target and nothing scrolls sideways.
- **Settings repeats "Media servers" as the section title and the group legend.** Different roles
  (heading and fieldset); harmless.
- **The finish page and the starting page poll while open.** A hidden tab keeps polling until the job ends,
  then stops. Cost is a few tiny requests.
- **Indexer entries keep the order they were added in** (no sort). Few entries are expected.
- **Screen readers were not run** (no VoiceOver or NVDA here). The live-region fixes follow the rules but a
  human check belongs in the owner's Plan 5 checklist.
- **Zoom to 200%** was not run separately; the 360 px pass covers reflow (WCAG 1.4.10) at roughly 355% zoom.

## Carry-forward

Plan 4:
- Home becomes search and requests. Add the disk-space warning (spec section 9) and an "Update Orama" button
  that uses the Agent's `self-update`.
- Webhooks from Sonarr and Radarr to Orama need `orama-server` in the Host allowlist and their own
  shared-secret check. They have no session and no CSRF token.

Plan 5:
- Run the real browser wizard on the VMs (spec 10.1.3) and on the NAS, including a real Plex approval.
- The owner's checklist gains "open the installer's link from a phone".
- Verify the Docker userland-proxy source-address caveat above.
- Run a screen reader (VoiceOver or NVDA) over the checklist and the confirm pages.
- After the `browser` CI job has been green three times, add it to `publish`'s `needs`.

## Deferred minors not fixed

- `forms.choice` compares values as strings; callers pass strings.
- The static files are cached for a day with a manual `v=` number in `base.html` (CSS is `v=7`). Bump it when
  an asset changes.
- If a second tab starts an apply while another page is polling, the poller notices the different job and
  sends the whole list. A job with more than 500 lines drops the oldest, and a page that already showed them
  would append from a slightly wrong place. Apply output is a few dozen lines, so this was left.
- The wizard's "Done" label is by position for every step except Indexers (every other step has working
  answers from the installer by the time you pass it).
- A pre-existing duplicate: `/plex/sign-in/status` and `/setup/finish` poll separately while a job runs.
- axe-core is optional in the browser test (`ORAMA_TEST_AXE=<axe.min.js>`); it is not a CI dependency.

## Final fix wave (after the whole-plan review)

- **Media folder is read-only in the web app (decision for the owner).** The Agent's only data
  mount and `ORAMA_DATA_ROOT` are fixed when the installer writes `core.compose.yaml`, so a
  folder typed in the wizard or Settings could never be used. The Folders step now only shows
  the folder and says to run the installer again with `ORAMA_DATA_ROOT` set; Settings shows it
  without a Save button (no `/settings/folders` save route). `bootstrap` moves a saved setup
  and draft to the folder it is given (a note on stderr), the installer allows a different
  data folder only when `ORAMA_DATA_ROOT` is set on purpose, Install keeps the live folder,
  and `apply` turns the Agent's refusal of a data folder into a plain sentence. Making the
  folder editable from the web would need a root-side rewrite of the Agent's compose.
- **Change setup keeps typed secrets in the draft** (`src/orama/web/draft.py`): while
  `setup.json` exists, wizard writes go to `draft:set:` / `draft:del:` names and are read over
  the live store; Install and the first apply move them in one `store.update` under
  `setup.lock`; Throw it away drops every `draft:` name and tests again (shared
  `undo_candidate`, apply.lock held, setup.lock not) the live entries the draft's tests sent
  (`draft:touched:`). A draft test's `test-ok` stays in the draft; its sent markers go live.
- Redaction hides only the values of secret names (`secrets.is_secret_name`): bookkeeping
  names (`remove:`, `test-ok:`, sent markers, `user:`, `plex:`...) are shown as they are.
