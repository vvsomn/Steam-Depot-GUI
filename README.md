# Steam Depot GUI

Steam Depot GUI is a tool that finds, downloads, and adds Steam depot manifests into a GreenLuma-preconfigured Steam installation. It streamlines manifest hunting across community repositories, automates GreenLuma integration steps, and keeps your injected titles organized with a modern, status-rich interface.

## 🎥 Showcase

https://github.com/user-attachments/assets/fb9c10b4-a5da-4b4a-8ca7-aab64e63e120

## Features
- **Steam-wide search with offline caching:** Builds a local FTS5 index from the Steam API and SteamSpy so you can instantly search by name or app ID, even delisted games.
- **Repository-aware manifest discovery:** Reads every GitHub repository listed in `repos.txt`, inspects per-app branches, and downloads the relevant `.lua` and `.key` files before downloading the latest manifest data.
- **One-click GreenLuma integration:** Copies manifests into `depotcache`, injects depot keys into `config.vdf`, generates AppList entries, creates `appmanifest_*.acf` files, and logs every step.
- **Drag-and-drop zip importer:** Accepts `.zip` bundles containing Lua/manifest/key files, validates their contents, resolves the matching app name, and runs the same GreenLuma integration flow entirely offline.
- **Persistent library management:** Stores results in `added_games.json`, presents an “Added Games” panel, and lets you remove titles while cleaning up manifests, AppList entries, depot keys, and resequencing the remaining files.
- **Steam restart helper:** Kills `steam.exe` and launches `DLLInjector.exe` from your Steam directory so updated manifests take effect with one button.
- **Configurable Steam path & UI polish:** Auto-detects Steam via the registry, allows manual browsing, surfaces progress/status messages, and provides a drop-zone overlay to guide imports.

## Use Cases
- Fetch depot manifests for a title directly from community GitHub repos and push them into GreenLuma.
- Import a ready-made zip package of manifests/keys then add it with one drag-and-drop.
- Prepare a curated set of depot entries, resequence AppList files automatically, and restart Steam with `DLLInjector.exe` to activate the new configuration.

## Prerequisites
- A Windows Steam install that already has GreenLuma **preconfigured**.
- `DLLInjector.exe` placed inside the Steam directory.
- Steam signed out before you run the injector helper.

## Getting Started
1. Clone or extract the project.
2. Launch the app with `python steam_depot_gui.py` (or run the packaged executable) and confirm the detected Steam folder, updating it if necessary.
3. Search by game name or app ID, select the repository branch result, and click **Add Selected** to download and integrate the manifests.
4. (Optional) Edit the `repos.txt` file to add any GitHub repository you want that hosts Lua/manifest files (one `owner/repo` per line).
5. (Optional) Drag a `.zip` archive onto the window to import a prepared package; the UI will confirm success and log any missing files.

## Troubleshooting
- If Steam path detection fails, use the **Browse** button to point at your Steam root.
- A missing Lua file in a repository stops integration—double-check that the selected branch follows the expected naming convention.
- When remote manifest downloads report 404 errors, the referenced manifest may have been removed upstream; retry later or fall back to an offline zip.
- If GreenLuma does not recognize a newly added game, verify that GreenLuma itself is already configured and run the **Restart Steam** helper to relaunch with the injector.
