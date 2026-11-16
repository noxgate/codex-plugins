# NoxGate Codex Plugins

This repository is a distribution mirror of the NoxGate plugin catalog. Edit the authoritative NoxGate plugins/shared and plugin sources, then run scripts/sync-plugin-distribution.py in that repository. Do not maintain separate runtime implementations here.

## Install and authorize

Add the marketplace noxgate/codex-plugins through your Codex plugin manager and install noxgate-media or noxgate-game-assets. After installation, use the real plugin directory:

~~~bash
python3 /absolute/path/to/plugin/bin/install.py
python3 /absolute/path/to/plugin/bin/noxgate auth login --profile work --agent codex
~~~

For game asset processing add --game-assets to install.py. The default CLI needs only Python 3.11+; optional dependencies use an isolated environment, and the installer prints the standalone noxgate command location. The browser asks you to sign into NoxGate and select or create your API Key. The CLI saves a separately revocable plugin credential in the user's private file store by default; it never defaults to Codex provider credentials. macOS uses ~/Library/Application Support/NoxGate, Linux uses the XDG configuration directory, and Windows uses LOCALAPPDATA (APPDATA as fallback). NOXGATE_HOME overrides the configuration directory.

Use noxgate auth status, list, use work, logout or revoke. NOXGATE_PROFILE selects a profile. Explicit auth import-codex only imports the public gateway address and still requires browser consent; no existing API Key is copied. Self-hosted deployments pass --gateway and --web. File storage is the default on all platforms. For explicit --store system, first install the optional keyring dependency using install.py --system-keyring; unavailable native storage fails closed without fallback. Explicit --store portable-env --env-file /absolute/path/to/plugin/.env stores only plugin credentials in a private plaintext file, never auto-discovers .env files, and reports the plaintext warning in status. The env backend remains read-only external injection. auth migrate-storage explicitly migrates between backends after verification and keeps the source by default. Initial authorization requires a browser on the same machine and completes automatically through a loopback callback protected by PKCE and state. Device codes, manual code entry and authorization polling are not provided; authorization secrets must never enter Agent output.

Task recovery keeps media bytes in a private cache; signed download URLs only enter file/system reference storage and are deleted after a successful download. portable-env and env do not persist recovery URLs; a synchronous response without a task ID cannot be recovered if its first download fails before caching. Task state contains an opaque recovery ID, never a full response or secret. Existing async tasks resume by their original ID without a new paid request. Explicit legacy migration: noxgate tasks migrate-state --state /absolute/path/task.json --profile work. Old game asset states additionally require --asset-root and --source-gateway. Migration validates gateway identity and online task ownership, and never reads an old Codex key. auth ensure honors explicit --gateway, --web, --agent, --store, --env-file and --scope; omitted settings retain the existing profile.

The generic CLI can be reused by other local Agents. WorkBuddy host installation and native secret-manager integration remain unverified. Linux Secret Service and Windows Credential Manager still need native platform validation; macOS Keychain has a synthetic credential round-trip test.

Plugin directories, their lib/bin runtime copies and the marketplace catalog are synchronized locally. Version 0.2.0 requires the matching NoxGate independent-auth backend and authorization page. See the GitHub release notes for published versions and validation limits.
