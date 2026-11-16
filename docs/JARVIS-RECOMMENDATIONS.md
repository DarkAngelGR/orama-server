# Jarvis recommendation engine — first implementation slice

This module is intentionally **offline and side-effect free**. It gives Orama a
deterministic, testable baseline for "For you" recommendations and a strict
validation boundary for Gemini/Ollama ranking output.

## Implemented

- `orama.recommendations.rank(watched, candidates, excluded=...)`
  consumes normalized history and **real catalogue candidates**. Explicit likes
  and ratings >=7 are weighted more strongly than merely finishing a title;
  continue-watching by itself does not imply a positive vote.
- Watched items, existing library IDs (through `excluded`), duplicate titles
  by source ID, malformed entries and unsupported media kinds are dropped.
- `apply_model_ranking(...)` allows a model to reorder known IDs only; its
  invented titles and text are never rendered. All user-facing strings must
  additionally be escaped by the template renderer.

## Next integration steps (not yet implemented)

1. Pull watch history and library IDs from Plex/Jellyfin using the already
   configured server credentials. Preserve user/profile isolation.
2. Fetch actual discovery candidates from an approved metadata provider (TMDB),
   with fixed outbound hosts, response-size/time limits, and secrets storage.
3. Add a scheduled, rate-limited cache refresh on the NAS; store per-user
   results without exposing Gemini or TMDB secrets to the browser.
4. Add an authenticated `For you` route and a Home UI section that links
   existing movie/series request pages. No automatic downloads.
5. Let the Jarvis process on the UN1250P ask Gemini to reorder candidate IDs,
   with Ollama/local ranking fallback, not unrestricted agent execution.
6. Add end-to-end, SSRF, model-output, cache, stale-result and accessibility
   tests, then run CI and the owner's deferred security review.

Do not expose Ollama on a public interface; use a firewall and an
authenticated, narrowly-scoped NAS-to-mini-PC channel.
