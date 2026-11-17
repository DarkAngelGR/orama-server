# Existing-app request routing

State: needs review
Owner: Codex

## Report

- Fixed in 5aba065; regression fixture corrected in 1db9bfe.
- MediaClient now uses the existing wiring address resolver for adopted Radarr, Sonarr and Bazarr, preserving their saved NAS address and host ports. Managed apps retain existing routing.
- Failed adopted-app connections do not fall back to a different Docker service hostname with the same credentials.
- Six routing regression cases failed before the fix. All 26 request-client tests passed afterward; Ruff and ShellCheck passed.
- The wider requests suite encountered Windows filesystem failures and was interrupted. Linux unit, browser, images, integration and Ubuntu/Debian end-to-end checks all passed in CI 44157159080. Publish skipped on test.
- https://github.com/DarkAngelGR/orama-server/actions/runs/44157159080
- No Agent permissions or Docker networks changed. NAS deployment and existing Radarr API authentication remain unverified.
