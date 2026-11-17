# Existing app API keys implementation plan

Goal: implement the approved Settings App connections design.
Spec: docs/superpowers/specs/2026-11-18-existing-app-api-keys-design.md
Architecture: existing Settings rendering plus a small credential-probe module using Http and SecretStore. No dependencies or Agent changes.

## Global constraints
Authenticated owner and CSRF required. Only enabled adopted allowlisted apps. Never echo secrets. Preserve saved key on failure. No apply or app writes.

## Review focus
Rejected/redirected/malformed responses, simultaneous setup changes, blank or control-character input, unconfigured host, and immediate request-client visibility.

## Task 1
- Add tests/web/test_app_connections.py: saved key survives invalid, unreachable and malformed results; success is immediately read by MediaClient; CSRF, sign-in, draft, busy and app allowlist; no secret in rendered output.
- Run tests and observe failure before implementation.
- Add src/orama/web/app_connections.py: fixed app metadata and probe_key(app, setup, key, http), requiring expected authenticated response shapes.
- Integrate POST /settings/app-connections/<app> with existing apply/setup locks, test while locked and atomically save only on success. Render forms through settings/_app_connections.html included in settings/index.html.
- Run focused tests, settings tests, Ruff and ShellCheck. Push to test and verify Linux CI; report NAS verification separately.
