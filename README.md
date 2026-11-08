# Orama Server

Orama Server is a control application and web console for deploying and managing an integrated home media stack (Plex, Jellyfin, Sonarr, Radarr, Prowlarr, SABnzbd, qBittorrent, and Gluetun VPN).

## Features

- **Dashboard**: Unified control plane displaying service availability, health, and links to web UIs.
- **Setup Wizard**: Guided step-by-step configuration for media servers, storage paths, download clients, playback device profiles, and subtitle automation.
- **Settings Console**: In-place management for indexers, Usenet servers, network host names, and security credentials.
- **Live Status**: Lightweight HTMX polling for near-instant status reflections.

## Development

```bash
npm install
ORAMA_DEMO_PASSWORD='<choose-a-unique-password>' npm run dev
```

This Node entry point is an in-memory UI demo, not the Python production service.
It binds only to `http://127.0.0.1:3000`. Supply a unique password of at least
12 characters through `ORAMA_DEMO_PASSWORD` for both development and `npm start`.
Demo changes reset on restart. Do not expose this demo through a public proxy.

Run `npm test` for authentication and packaged-build regression checks.
