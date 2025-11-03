# Steam Depot GUI

Steam Depot GUI helps you catalogue, download, and organize Steam depot manifests inside a Green Luma-preconfigured Steam installation. It streamlines manifest discovery across community repositories, automates common Green Luma file-management steps, and keeps your personal collection tidy with a modern, status-rich interface.

This project is intended for maintaining legitimate backups of titles you own. It does not ship or endorse any tools that interfere with Steam licensing.

## Showcase

https://github.com/user-attachments/assets/fb9c10b4-a5da-4b4a-8ca7-aab64e63e120

## Features
- **Steam-wide search with offline caching:** Builds a local FTS5 index from the Steam API and SteamSpy so you can instantly search by name or app ID, even delisted games.
- **Repository-aware manifest discovery:** Reads every GitHub repository listed in `repos.txt`, inspects per-app branches, and downloads the relevant `.lua` and `.key` files before pulling the latest manifest data.
- **Guided Green Luma setup:** Copies manifests into `depotcache`, updates depot key data, generates AppList entries, creates `appmanifest_*.acf` files, and logs every step.
- **Drag-and-drop zip importer:** Accepts `.zip` bundles containing Lua/manifest/key files, validates their contents, resolves the matching app name, and runs the same Green Luma workflow entirely offline.
- **Persistent library management:** Stores results in `added_games.json`, presents an Added Games panel, and lets you remove titles while cleaning up manifests, AppList entries, depot keys, and resequencing the remaining files.
- **Configurable Steam path & UI polish:** Auto-detects Steam via the registry, allows manual browsing, surfaces progress/status messages, and provides a drop-zone overlay to guide imports.

## Use Cases
- Fetch depot manifests for a title directly from community GitHub repos and add them to your Green Luma setup.
- Import a ready-made zip package of manifests or keys and process it with a single drag-and-drop.
- Prepare a curated set of depot entries, resequence AppList files automatically, and keep your Steam library metadata organized.

## Prerequisites
- A Windows Steam install that already has Green Luma preconfigured.
- Green Luma files placed inside your Steam directory (for example `C:\Program Files (x86)\Steam`).
- Steam signed out before you apply changes to a Green Luma configuration.

## Getting Started
1. Clone or extract the project.
2. Launch the app with `python steam_depot_gui.py` (or run the packaged executable) and confirm the detected Steam folder, updating it if necessary.
3. Search by game name or app ID, select the repository branch result, and click **Add Selected** to download and integrate the manifests.
4. (Optional) Edit the `repos.txt` file to add any GitHub repository you want that hosts Lua or manifest files (one `owner/repo` per line).
5. (Optional) Drag a `.zip` archive onto the window to import a prepared package; the UI will confirm success and log any missing files.

## Troubleshooting
- If Steam path detection fails, use the **Browse** button to point at your Steam root.
- A missing Lua file in a repository stops integration. Double-check that the selected branch follows the expected naming convention.
- When remote manifest downloads report 404 errors, the referenced manifest may have been removed upstream. Retry later or fall back to an offline zip.
