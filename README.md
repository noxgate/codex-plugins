# NoxGate Codex Plugins

This repository distributes two Codex plugins maintained by NoxGate:

- `noxgate-media`: image, video, and audio creation.
- `noxgate-game-assets`: game image and audio asset production.

## Install

Add the marketplace once, then install either or both plugins:

```bash
codex plugin marketplace add noxgate/codex-plugins
codex plugin add noxgate-media@noxgate
codex plugin add noxgate-game-assets@noxgate
```

The plugins use the current Codex provider configuration. They do not include API keys, base URLs, or project-specific data.

## Repository layout

- `.agents/plugins/marketplace.json`: marketplace catalog.
- `plugins/noxgate-media/`: general media plugin.
- `plugins/noxgate-game-assets/`: game asset plugin.

Each plugin has its own version and changelog. Plugin updates are published by updating this repository's plugin directories and pushing the new versions.
