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
npm run dev
```

Server runs on `http://0.0.0.0:3000`.
