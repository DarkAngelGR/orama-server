# Orama Server

Orama Server sets up and looks after a home media server for you. You run one command on a
Debian, Ubuntu or UGREEN (UGOS Pro) machine, answer a few questions in your browser, and Orama
installs and connects the apps: Plex and/or Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd,
qBittorrent and a VPN. After that you search for a movie or a show in Orama, ask for it, and
watch its progress until it is ready to play, with subtitles.

## Install

On the server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/DarkAngelGR/orama-server/main/install.sh | sudo bash
```

The installer ([`install.sh`](install.sh)) uses the Docker that is already there (or installs it
on plain Debian/Ubuntu), starts Orama, and prints a one-time setup link. Open that link in a
browser to choose a password and finish the setup. Running the installer again is safe: it only
repairs what is missing.

## How it is built

- **Orama** is a Python (Flask) web app that runs in a Docker container. It has no access to
  Docker itself.
- **The Orama Agent** is a small helper container, with no network, that is the only piece
  allowed to control Docker. It accepts a short, fixed list of commands from Orama and checks
  every one of them.
- The media apps run as ordinary Docker Compose containers that you can also manage by hand.

The source is in [`src/orama/`](src/orama) (the app) and [`src/orama_agent/`](src/orama_agent)
(the Agent); the container images are built from [`docker/`](docker).

## Working on Orama

- [`AGENTS.md`](AGENTS.md): the rules for anyone (person or AI assistant) changing this repo,
  including the checks to run before every commit.
- [`docs/STATUS.md`](docs/STATUS.md): what is done, what is in progress and what comes next.
- [`docs/superpowers/specs/2026-11-03-orama-release-1-design.md`](docs/superpowers/specs/2026-11-03-orama-release-1-design.md):
  the design for Release 1.
