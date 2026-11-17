# Existing app API credentials

State: needs review
Owner: Codex

Instructions: docs/superpowers/plans/2026-11-18-existing-app-api-keys.md
Approved design: docs/superpowers/specs/2026-11-18-existing-app-api-keys-design.md

## Report

- Implemented in cdec2ce.
- Settings → App connections lists enabled adopted Radarr, Sonarr, Prowlarr, Bazarr, SABnzbd and Jellyfin. Test and save checks the key with a read-only request to the saved address. A verified key is stored as `apikey:<app>` and media requests use it immediately. A blank, rejected, unreachable, redirected or unreadable answer keeps the previous key and does not show the submitted one.
- Saving a key does not apply, restart apps, or change those apps' own settings.
- Local checks: 21 app-connection tests, 77 settings tests, and Ruff. No shell files changed.
- CI 44164818162 on cdec2ce passed unit, images, integration, and Ubuntu and Debian end-to-end. The browser job failed once on the existing wizard checklist timing check at 360px, then passed on rerun. Publish skipped.
- https://github.com/DarkAngelGR/orama-server/actions/runs/44164818162
- The NAS does not have this commit yet, and a real key has not been saved.
