import logging
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from collections import OrderedDict

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk


USER_AGENT = "SteamDepotGUI"
SEARCH_DELAY_MS = 400
STATUS_IDLE = "Idle"
DEFAULT_LANGUAGE = "english"
REMOTE_MANIFEST_URL_TEMPLATE = "https://github.com/qwe213312/k25FCdfEOoEJ42S6/raw/refs/heads/main/{depot}_{manifest}.manifest"


LUA_DEPOT_PATTERN = re.compile(r'addappid\(\s*(\d+)\s*,\s*\d+\s*,\s*"([^"]+)"\)', re.IGNORECASE)
LUA_SET_MANIFEST_PATTERN = re.compile(r'setmanifestid\(\s*(\d+)\s*,', re.IGNORECASE)


logger = logging.getLogger("SteamDepotGUI")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


def _format_vdf_key(key):
    text = str(key)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text


def _format_vdf_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text


def dump_vdf(data, indent=0):
    lines = []
    indentation = "\t" * indent
    for key, value in data.items():
        formatted_key = _format_vdf_key(key)
        if isinstance(value, dict):
            lines.append(f'{indentation}"{formatted_key}"')
            lines.append(f"{indentation}{{")
            lines.extend(dump_vdf(value, indent + 1))
            lines.append(f"{indentation}}}")
        else:
            lines.append(f'{indentation}"{formatted_key}"\t\t"{_format_vdf_value(value)}"')
    return lines


def iter_lua_depot_entries(content):
    for depot_id, key in LUA_DEPOT_PATTERN.findall(content):
        yield depot_id, key


def iter_lua_manifest_depot_ids(content):
    for match in LUA_SET_MANIFEST_PATTERN.finditer(content):
        yield match.group(1)


def format_launcher_path(path_obj):
    text = str(path_obj)
    if os.name == "nt":
        candidate = Path(text)
        candidate = candidate.with_name("steam.exe")
        resolved = str(candidate)
        return resolved.lower()
    return text


def read_repos(path):
    items = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                items.append(line)
    return items


def request_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        text = resp.read().decode(charset, errors="ignore")
        return json.loads(text)


def request_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _import_steam_client():
    try:
        from steam.client import SteamClient  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "The 'steam' package is required to fetch depot manifests. Install it with 'pip install steam'."
        ) from exc
    return SteamClient


def get_apps_dict(client, appids, cache):
    normalized = []
    for appid in appids:
        try:
            app_int = int(appid)
        except (TypeError, ValueError):
            continue
        normalized.append(app_int)
    missing = [
        appid
        for appid in normalized
        if appid not in cache and str(appid) not in cache
    ]
    if not missing:
        return cache
    result = client.get_product_info(apps=missing) or {}
    cache.update(result.get("apps", {}))
    return cache


def get_app_entry(apps_dict, appid):
    return apps_dict.get(appid) or apps_dict.get(str(appid), {})


def pick_latest_manifest(depot_info, app_branches):
    manifests = (depot_info or {}).get("manifests", {})
    if not manifests:
        return None, None, None

    def extract_gid(manifest):
        if isinstance(manifest, dict):
            return manifest.get("gid")
        return manifest

    def branch_timestamp(branch):
        return (app_branches or {}).get(branch, {}).get("timeupdated")

    def gid_as_int(manifest):
        val = extract_gid(manifest)
        try:
            return int(val)
        except (TypeError, ValueError):
            return -1

    if "public" in manifests:
        gid_val = extract_gid(manifests["public"])
        if gid_val is not None:
            branch = "public"
            timestamp = branch_timestamp(branch)
            return str(gid_val), branch, timestamp

    best = None
    best_time = -1
    for branch, manifest in manifests.items():
        ts = int(branch_timestamp(branch) or 0)
        if ts > best_time:
            best_time = ts
            gid_val = extract_gid(manifest)
            if gid_val is not None:
                best = (str(gid_val), branch, ts)
    if best:
        return best

    best_branch = None
    best_gid_value = None
    best_gid_int = -1
    for branch, manifest in manifests.items():
        gid_int = gid_as_int(manifest)
        if gid_int > best_gid_int:
            gid_val = extract_gid(manifest)
            if gid_val is None:
                continue
            best_gid_int = gid_int
            best_branch = branch
            best_gid_value = gid_val
    if best_gid_value is not None:
        return str(best_gid_value), best_branch, None
    return None, None, None


def os_badges(depot):
    oslist = (depot.get("config") or {}).get("oslist", "")
    badges = []
    if "windows" in oslist or oslist in ("", "windows"):
        badges.append("Windows")
    if "macos" in oslist or oslist == "macos":
        badges.append("macOS")
    if "linux" in oslist or oslist == "linux":
        badges.append("Linux")
    return badges


def fetch_latest_windows_manifests(appid):
    SteamClient = _import_steam_client()
    try:
        app_id_int = int(appid)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid app id {appid}: {exc}") from exc
    client = SteamClient()
    logger.info("Connecting to Steam anonymously to resolve manifests for app %s", app_id_int)
    client.anonymous_login()
    apps_cache = {}
    try:
        get_apps_dict(client, [app_id_int], apps_cache)
        app_entry = get_app_entry(apps_cache, app_id_int)
        info = app_entry.get("appinfo", app_entry) or {}
        depots = info.get("depots", {})
        app_branches = depots.get("branches", {})

        rows = []
        need_fetch = set()
        for key, value in depots.items():
            if not str(key).isdigit():
                continue
            depot_id = int(key)
            src_app = value.get("depotfromapp")
            if src_app:
                try:
                    need_fetch.add(int(src_app))
                except (TypeError, ValueError):
                    pass
            rows.append((depot_id, value, src_app))

        if need_fetch:
            logger.info("Fetching shared depot sources for apps: %s", ", ".join(str(x) for x in sorted(need_fetch)))
            get_apps_dict(client, list(need_fetch), apps_cache)

        manifests = []
        for depot_id, depot_entry, src_app in rows:
            depot_record = depot_entry
            branches_meta = app_branches
            if src_app:
                src_entry = get_app_entry(apps_cache, int(src_app))
                src_info = src_entry.get("appinfo", src_entry) or {}
                src_depots = src_info.get("depots", {})
                branches_meta = src_depots.get("branches", {}) or branches_meta
                depot_record = src_depots.get(str(depot_id), {}) or src_depots.get(depot_id, {})

            if not depot_record:
                logger.info("Skipping depot %s: no accessible record", depot_id)
                continue

            if "Windows" not in os_badges(depot_record):
                logger.info("Skipping depot %s: not flagged for Windows", depot_id)
                continue

            gid, branch, timestamp = pick_latest_manifest(depot_record, branches_meta or {})
            if not gid:
                logger.info("No visible manifest for Windows depot %s", depot_id)
                continue

            manifests.append(
                {
                    "depot_id": str(depot_id),
                    "manifest_id": str(gid),
                    "branch": branch or "public",
                    "timestamp": timestamp,
                }
            )
        logger.info(
            "Resolved %d Windows depot manifest(s) for app %s",
            len(manifests),
            app_id_int,
        )
        return manifests
    finally:
        try:
            client.logout()
        except Exception:
            pass


def download_remote_manifest(depot_id, manifest_id, dest_dir):
    url = REMOTE_MANIFEST_URL_TEMPLATE.format(depot=depot_id, manifest=manifest_id)
    logger.info("Downloading manifest %s for depot %s from %s", manifest_id, depot_id, url)
    try:
        data = request_bytes(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"Manifest {manifest_id} for depot {depot_id} is not available from the manifest repository."
            ) from exc
        raise
    target = dest_dir / f"{depot_id}_{manifest_id}.manifest"
    target.write_bytes(data)
    logger.info("Stored manifest for depot %s at %s", depot_id, target)
    return target



def detect_steam_path():
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                value = winreg.QueryValueEx(key, "SteamPath")[0]
                return Path(value).expanduser()
        except OSError:
            try:
                import winreg

                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Valve\Steam") as key:
                    value = winreg.QueryValueEx(key, "InstallPath")[0]
                    return Path(value).expanduser()
            except OSError:
                pass
        return Path(r"C:\Program Files (x86)\Steam")
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Steam"
    return Path.home() / ".steam/steam"


def ensure_icon(root):
    icon_path = Path("steam_icon.ico")
    if icon_path.exists():
        try:
            root.iconbitmap(str(icon_path.resolve()))
        except Exception:
            pass


def hex_to_rgb(code):
    code = code.lstrip("#")
    return tuple(int(code[i : i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(values):
    return "#{:02x}{:02x}{:02x}".format(*values)


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent,
        text,
        command=None,
        width=None,
        height=40,
        radius=16,
        fill="#66c0f4",
        hover="#7fd1ff",
        active="#55addd",
        text_color="#0a111b",
        font=("Segoe UI Semibold", 10),
        background=None,
    ):
        parent_bg = background
        if parent_bg is None:
            try:
                parent_bg = parent.cget("background")
                if not parent_bg:
                    raise ValueError
            except Exception:
                parent_bg = "#152131"
        super().__init__(parent, highlightthickness=0, bd=0, bg=parent_bg, cursor="hand2")
        self.text = text
        self.command = command
        self.radius = radius
        self.normal_color = fill
        self.hover_color = hover
        self.active_color = active
        self.text_color = text_color
        self.font = tkfont.Font(font=font)
        text_width = self.font.measure(self.text)
        padding_x = 32
        if width is None:
            width = text_width + padding_x
        self.configure(width=width, height=height)
        self.bind("<Enter>", self._handle_enter)
        self.bind("<Leave>", self._handle_leave)
        self.bind("<ButtonPress-1>", self._handle_press)
        self.bind("<ButtonRelease-1>", self._handle_release)
        self.draw(self.normal_color)

    def draw(self, color):
        self.delete("button")
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        radius = min(self.radius, height // 2)
        self._round_rect(2, 2, width - 2, height - 2, radius, fill=color, outline="")
        self.create_text(
            width // 2,
            height // 2,
            text=self.text,
            font=self.font,
            fill=self.text_color,
            tags=("button",),
        )

    def _round_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        return self.create_polygon(points, smooth=True, splinesteps=20, tags=("button",), **kwargs)

    def _handle_enter(self, _):
        self.draw(self.hover_color)

    def _handle_leave(self, _):
        self.draw(self.normal_color)

    def _handle_press(self, _):
        self.draw(self.active_color)

    def _handle_release(self, _):
        self.draw(self.hover_color)
        if self.command:
            self.command()

    def set_text(self, text):
        self.text = text
        text_width = self.font.measure(self.text)
        padding_x = 32
        width = text_width + padding_x
        self.configure(width=width)
        self.draw(self.normal_color)


class LocalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = raw
            except Exception:
                self.data = {}

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def list_games(self):
        for repo, mapping in self.data.items():
            for appid, info in mapping.items():
                yield repo, appid, info.get("name", f"App {appid}"), info

    def get(self, repo, appid):
        return self.data.get(repo, {}).get(appid)

    def add(self, repo, appid, name, files, depot_keys, applist_files=None):
        mapping = self.data.setdefault(repo, {})
        mapping[appid] = {
            "name": name,
            "files": files,
            "depot_keys": depot_keys,
            "applist_files": list(applist_files or []),
        }
        self.save()
        logger.info(
            "Recorded %s (%s) with %d manifest(s), %d depot key(s), and %d AppList file(s) for repo %s",
            name,
            appid,
            len(files or []),
            len(depot_keys or {}),
            len(applist_files or []),
            repo,
        )

    def remove(self, repo, appid):
        mapping = self.data.get(repo)
        if not mapping:
            return None
        info = mapping.pop(appid, None)
        if not mapping:
            self.data.pop(repo, None)
        self.save()
        logger.info("Removed %s (%s) from local store", repo, appid)
        return info

    def update_applist_files(self, rename_map):
        if not rename_map:
            return
        updated = False
        for repo_mapping in self.data.values():
            for info in repo_mapping.values():
                files = info.get("applist_files") or []
                if not files:
                    continue
                new_files = []
                changed = False
                for name in files:
                    new_name = rename_map.get(name, name)
                    if new_name != name:
                        changed = True
                    new_files.append(new_name)
                if changed:
                    info["applist_files"] = new_files
                    updated = True
        if updated:
            self.save()
            logger.info("Updated AppList references in local store after resequencing.")


class ManifestRepo:
    def __init__(self, identifier):
        self.identifier = identifier
        self.branch_cache = {}
        self.tree_cache = {}

    def branch_exists(self, branch):
        if branch in self.branch_cache:
            return self.branch_cache[branch]
        url = f"https://api.github.com/repos/{self.identifier}/branches/{branch}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20):
                self.branch_cache[branch] = True
                logger.info("Branch %s exists in %s", branch, self.identifier)
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.branch_cache[branch] = False
                logger.info("Branch %s not found in %s", branch, self.identifier)
                return False
            raise

    def fetch_manifest_paths(self, branch):
        logger.info("Fetching manifest paths from %s (branch %s)", self.identifier, branch)
        tree = self.fetch_tree(branch)
        paths = []
        for entry in tree:
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            lower = path.lower()
            if not lower.endswith((".manifest", ".lua", ".key")):
                continue
            if "dlc" in lower:
                continue
            paths.append(path)
        if paths:
            logger.info("Got manifests for %s (branch %s): %s", self.identifier, branch, ", ".join(paths))
        else:
            logger.info("No manifest files found for %s (branch %s)", self.identifier, branch)
        return paths

    def fetch_tree(self, branch):
        cached = self.tree_cache.get(branch)
        if cached is not None:
            return cached
        url = f"https://api.github.com/repos/{self.identifier}/git/trees/{branch}?recursive=1"
        data = request_json(url)
        tree = data.get("tree", [])
        self.tree_cache[branch] = tree
        return tree

    def download_files(self, branch, paths, dest_dir):
        logger.info(
            "Downloading %d file(s) from %s (branch %s) into %s",
            len(paths),
            self.identifier,
            branch,
            dest_dir,
        )
        downloaded = []
        for relative in paths:
            raw_url = f"https://raw.githubusercontent.com/{self.identifier}/{branch}/{relative}"
            try:
                logger.info("Downloading %s", raw_url)
                data = request_bytes(raw_url)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    logger.info("Skipping missing file %s", raw_url)
                    continue
                raise
            target = dest_dir / Path(relative).name
            target.write_bytes(data)
            downloaded.append(target)
            logger.info("Downloaded %s to %s", relative, target)
        return downloaded


class GreenLumaIntegrator:
    def __init__(self, steam_root):
        self.steam_root = Path(steam_root)
        self.depotcache = self.steam_root / "depotcache"
        self.config_dir = self.steam_root / "config"
        self.config_path = self.config_dir / "config.vdf"
        self.applist = self.steam_root / "AppList"
        self.steamapps = self.steam_root / "steamapps"
        self.depotcache.mkdir(parents=True, exist_ok=True)
        self.applist.mkdir(parents=True, exist_ok=True)
        self.steamapps.mkdir(parents=True, exist_ok=True)

    def apply(self, app_id, app_name, files, allowed_depots=None, progress_callback=None):
        logger.info("Applying manifests and keys for %s (%s)", app_id, app_name)
        depot_keys = {}
        manifest_files = []
        allowed_set = {str(depot) for depot in (allowed_depots or [])}
        if allowed_set:
            logger.info("Restricting integration to Windows depots: %s", ", ".join(sorted(allowed_set)))
        def report(message):
            if not progress_callback:
                return
            try:
                progress_callback(message)
            except Exception:
                logger.exception("Progress callback failed")
        manifest_reported = False
        for path in files:
            suffix = path.suffix.lower()
            if suffix == ".manifest":
                depot_from_name = None
                stem = path.stem
                if "_" in stem:
                    candidate = stem.split("_", 1)[0]
                    if candidate.isdigit():
                        depot_from_name = candidate
                if allowed_set and depot_from_name not in allowed_set:
                    logger.info(
                        "Skipping manifest %s: depot %s not in Windows depot list",
                        path.name,
                        depot_from_name or "?",
                    )
                    continue
                if not manifest_reported:
                    report("Moving manifests")
                    manifest_reported = True
                target = self.depotcache / path.name
                logger.info("Moving manifest %s to %s", path, target)
                shutil.copy2(path, target)
                logger.info("Moved manifest %s to %s", path.name, target)
                manifest_files.append(target.name)
            elif suffix == ".lua":
                content = path.read_text(encoding="utf-8", errors="ignore")
                for depot_id, key in iter_lua_depot_entries(content):
                    if allowed_set and depot_id not in allowed_set:
                        logger.info("Skipping key for depot %s (not Windows)", depot_id)
                        continue
                    if depot_id and key:
                        depot_keys[depot_id] = key
                        logger.info("Got key for depot %s from %s", depot_id, path.name)
            elif suffix == ".key":
                for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(";", 1)
                    if len(parts) != 2:
                        continue
                    depot_id, key = parts[0].strip(), parts[1].strip()
                    if allowed_set and depot_id not in allowed_set:
                        logger.info("Skipping key for depot %s from %s (not Windows)", depot_id, path.name)
                        continue
                    if depot_id.isdigit() and key:
                        depot_keys[depot_id] = key
                        logger.info("Got key for depot %s from %s", depot_id, path.name)
        if manifest_files:
            logger.info("Got manifests: %s", ", ".join(manifest_files))
        else:
            logger.info("No manifest files moved for app %s", app_id)
        if allowed_set:
            depot_keys = {k: v for k, v in depot_keys.items() if k in allowed_set}
        if depot_keys:
            logger.info("Adding keys to file config.vdf: %s", ", ".join(sorted(depot_keys)))
            report("Adding keys to Lua")
            self.update_config_vdf(depot_keys)
        report("Adding depots to AppList")
        applist_files, rename_map = self.create_applist_files(app_id, depot_keys)
        report(f"Making acf for {app_id}")
        self.create_appmanifest(app_id, app_name, manifest_files)
        return manifest_files, depot_keys, applist_files, rename_map

    def update_config_vdf(self, depot_keys):
        if not self.config_path.exists():
            logger.info("config.vdf not found; skipping depot key insertion.")
            return
        content = self.config_path.read_text(encoding="utf-8", errors="ignore")
        depots_index = content.find('"depots"')
        if depots_index == -1:
            logger.info("No depots section found in config.vdf; skipping depot key insertion.")
            return
        brace_start = content.find("{", depots_index)
        if brace_start == -1:
            return
        brace_count = 1
        brace_end = brace_start + 1
        while brace_count > 0 and brace_end < len(content):
            char = content[brace_end]
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
            brace_end += 1
        if brace_count != 0:
            return
        segment = content[depots_index:brace_end]
        entries = []
        added_ids = []
        for depot_id, key in depot_keys.items():
            if f'"{depot_id}"' in segment:
                continue
            entries.append(
                f'\n\t\t"{depot_id}"\n\t\t{{\n\t\t\t"DecryptionKey"\t\t"{key}"\n\t\t}}'
            )
            added_ids.append(depot_id)
        if not entries:
            logger.info("All depot keys already present in config.vdf; nothing to add.")
            return
        insert_pos = brace_end - 1
        updated = content[:insert_pos] + "".join(entries) + content[insert_pos:]
        self.config_path.write_text(updated, encoding="utf-8")
        logger.info("Added depot keys to config.vdf: %s", ", ".join(added_ids))

    def _scan_applist_entries(self):
        entries = []
        for path in self.applist.glob("*.txt"):
            try:
                number = int(path.stem)
            except Exception:
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                content = ""
            entries.append((number, path, content))
        entries.sort(key=lambda item: item[0])
        return entries

    def _content_to_filename_map(self):
        mapping = {}
        for _, path, content in self._scan_applist_entries():
            if content:
                mapping[content] = path.name
        return mapping

    def resequence_applist(self):
        entries = self._scan_applist_entries()
        if all(index == position for position, (index, _, _) in enumerate(entries)):
            return {}
        mapping = {}
        temp_paths = []
        epoch = time.time_ns()
        for new_index, (old_index, path, _) in enumerate(entries):
            original_name = path.name
            temp_path = path.parent / f"__tmp_{epoch}_{new_index}_{original_name}"
            path.rename(temp_path)
            temp_paths.append(
                (
                    temp_path,
                    path.parent / f"{new_index}.txt",
                    original_name,
                    old_index,
                    new_index,
                )
            )
        for temp_path, final_path, old_name, old_index, new_index in temp_paths:
            temp_path.rename(final_path)
            if old_index != new_index or old_name != final_path.name:
                mapping[old_name] = final_path.name
        if mapping:
            logger.info(
                "Resequenced AppList entries: %s",
                ", ".join(f"{old}->{new}" for old, new in sorted(mapping.items())),
            )
        return mapping

    def create_applist_files(self, app_id, depot_keys):
        logger.info(
            "Ensuring AppList entries for app %s with depots: %s",
            app_id,
            ", ".join(sorted(depot_keys)) if depot_keys else "none",
        )
        entries = self._scan_applist_entries()
        content_map = {}
        existing_numbers = []
        duplicate_paths = []
        for number, path, content in entries:
            existing_numbers.append(number)
            if content:
                if content in content_map:
                    duplicate_paths.append(path)
                else:
                    content_map[content] = path

        if duplicate_paths:
            logger.info(
                "Removing duplicate AppList entries for values: %s",
                ", ".join(path.read_text(encoding="utf-8", errors="ignore").strip() for path in duplicate_paths if path.exists()),
            )
            for path in duplicate_paths:
                try:
                    path.unlink()
                    logger.info("Deleted duplicate AppList file %s", path)
                except Exception:
                    logger.exception("Failed to delete duplicate AppList file %s", path)
            entries = self._scan_applist_entries()
            content_map = {}
            existing_numbers = []
            for number, path, content in entries:
                existing_numbers.append(number)
                if content:
                    content_map.setdefault(content, path)

        next_number = max(existing_numbers) + 1 if existing_numbers else 0
        values = [str(value) for value in list(sorted(depot_keys)) + [app_id]]
        for value in values:
            path = content_map.get(value)
            if path:
                logger.info("AppList entry already exists for %s at %s", value, path)
                continue
            target = self.applist / f"{next_number}.txt"
            target.write_text(value, encoding="utf-8")
            content_map[value] = target
            logger.info("Added AppList entry %s at %s", value, target)
            next_number += 1
        rename_map = self.resequence_applist()
        final_mapping = self._content_to_filename_map()
        applist_files = [final_mapping[value] for value in values if value in final_mapping]
        return applist_files, rename_map

    def _resolve_launcher_path(self):
        candidates = []
        if os.name == "nt":
            candidates = ["steam.exe", "Steam.exe"]
        else:
            candidates = ["steam.sh"]
        for name in candidates:
            candidate = self.steam_root / name
            if candidate.exists():
                return format_launcher_path(candidate)
        return ""

    def create_appmanifest(self, app_id, app_name, manifest_files=None):
        target = self.steamapps / f"appmanifest_{app_id}.acf"
        launcher_path = self._resolve_launcher_path()
        last_updated = int(time.time())
        app_state = OrderedDict()
        app_state["appid"] = str(app_id)
        app_state["Universe"] = "1"
        app_state["LauncherPath"] = launcher_path
        app_state["name"] = app_name
        app_state["StateFlags"] = "4"
        app_state["installdir"] = str(app_id)
        app_state["LastUpdated"] = str(last_updated)
        app_state["SizeOnDisk"] = "0"
        app_state["StagingSize"] = "0"
        app_state["buildid"] = "0"
        app_state["LastOwner"] = "0"
        app_state["UpdateResult"] = "0"
        app_state["BytesToDownload"] = "0"
        app_state["BytesDownloaded"] = "0"
        app_state["BytesToStage"] = "0"
        app_state["BytesStaged"] = "0"
        app_state["TargetBuildID"] = "0"
        app_state["AutoUpdateBehavior"] = "0"
        app_state["AllowOtherDownloadsWhileRunning"] = "0"
        app_state["ScheduledAutoUpdate"] = "0"
        user_config = OrderedDict()
        user_config["language"] = DEFAULT_LANGUAGE
        app_state["UserConfig"] = user_config
        mounted_config = OrderedDict()
        mounted_config["language"] = DEFAULT_LANGUAGE
        app_state["MountedConfig"] = mounted_config
        appmanifest = OrderedDict()
        appmanifest["AppState"] = app_state
        logger.info("Making acf for %s at %s", app_id, target)
        lines = dump_vdf(appmanifest)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Made acf for %s at %s", app_id, target)

    def remove_entry(self, app_id, depot_keys, manifest_files):
        logger.info("Removing manifests and keys for %s", app_id)
        for name in manifest_files or []:
            target = self.depotcache / name
            if target.exists():
                try:
                    target.unlink()
                    logger.info("Removed manifest %s", target)
                except Exception:
                    pass
        if depot_keys:
            self.remove_config_entries(list(depot_keys.keys()))
            self.remove_applist_entries(set(depot_keys.keys()) | {app_id})
        else:
            self.remove_applist_entries({app_id})
        manifesto = self.steamapps / f"appmanifest_{app_id}.acf"
        if manifesto.exists():
            try:
                manifesto.unlink()
                logger.info("Removed appmanifest %s", manifesto)
            except Exception:
                pass
        rename_map = self.resequence_applist()
        return rename_map

    def remove_config_entries(self, depot_ids):
        if not self.config_path.exists():
            return
        content = self.config_path.read_text(encoding="utf-8", errors="ignore")
        for depot_id in depot_ids:
            token = f'"{depot_id}"'
            idx = content.find(token)
            if idx == -1:
                logger.info("Depot %s not present in config.vdf; nothing to remove.", depot_id)
                continue
            brace_start = content.find("{", idx)
            if brace_start == -1:
                continue
            brace_count = 1
            brace_end = brace_start + 1
            while brace_count > 0 and brace_end < len(content):
                char = content[brace_end]
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                brace_end += 1
            if brace_count != 0:
                continue
            content = content[:idx] + content[brace_end:]
            logger.info("Removed depot %s from config.vdf", depot_id)
        self.config_path.write_text(content, encoding="utf-8")

    def remove_applist_entries(self, id_set):
        for path in list(self.applist.glob("*.txt")):
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if content in id_set:
                try:
                    path.unlink()
                    logger.info("Removed AppList entry %s", path)
                except Exception:
                    pass


class SteamDepotApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Steam Depot GUI")
        self.root.update_idletasks()
        self.root.geometry("1080x640")
        self.root.minsize(980, 560)
        ensure_icon(self.root)
        self.create_background()
        self.queue = queue.Queue()
        self.search_var = tk.StringVar()
        self.search_after_id = None
        self.status_var = tk.StringVar(value=STATUS_IDLE)
        self.download_log_var = tk.StringVar(value="")
        self.repos = read_repos(Path("repos.txt"))
        self.repo_objects = {identifier: ManifestRepo(identifier) for identifier in self.repos}
        self.store = LocalStore("added_games.json")
        steam_root = detect_steam_path()
        self.steam_folder_var = tk.StringVar(value=str(steam_root))
        self.integrator = GreenLumaIntegrator(steam_root)
        self.latest_results = {}
        self.active_search = None
        self.setup_style()
        self.build_gui()
        self.refresh_added()
        self.root.after(150, self.process_queue)

    def create_background(self):
        self.background_canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.background_canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas_window = None
        self.root.bind("<Configure>", self.on_resize)

    def draw_gradient(self, width, height):
        if width <= 0 or height <= 0:
            return
        top = hex_to_rgb("#0b141f")
        bottom = hex_to_rgb("#11263e")
        steps = max(height, 1)
        for i in range(height):
            ratio = i / steps
            r = int(top[0] + (bottom[0] - top[0]) * ratio)
            g = int(top[1] + (bottom[1] - top[1]) * ratio)
            b = int(top[2] + (bottom[2] - top[2]) * ratio)
            self.background_canvas.create_line(
                0,
                i,
                width,
                i,
                tags=("gradient",),
                fill=rgb_to_hex((r, g, b)),
            )

    def on_resize(self, event=None):
        if not hasattr(self, "background_canvas"):
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        self.background_canvas.delete("gradient")
        self.draw_gradient(width, height)
        if self.canvas_window is not None:
            margin = 24
            content_width = max(width - margin * 2, 200)
            content_height = max(height - margin * 2, 200)
            self.background_canvas.coords(self.canvas_window, margin, margin)
            self.background_canvas.itemconfigure(self.canvas_window, width=content_width, height=content_height)

    def setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base_bg = "#101a29"
        surface_bg = "#152131"
        panel_bg = "#182c44"
        accent = "#66c0f4"
        text_primary = "#e7f1ff"
        text_muted = "#7ca6c7"
        style.configure("Main.TFrame", background=base_bg)
        style.configure("Surface.TFrame", background=surface_bg)
        style.configure("Panel.TFrame", background=panel_bg)
        style.configure(
            "Panel.TLabelframe",
            background=panel_bg,
            foreground=accent,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Panel.TLabelframe.Label",
            background=panel_bg,
            foreground=accent,
            font=("Segoe UI Semibold", 11),
        )
        style.configure("Panel.TLabel", background=panel_bg, foreground=text_primary)
        style.configure("Header.TLabel", background=surface_bg, foreground=text_primary, font=("Segoe UI Semibold", 20))
        style.configure("Status.TLabel", background=surface_bg, foreground=text_muted, font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=surface_bg, foreground=accent, font=("Segoe UI Semibold", 12))
        style.configure(
            "Action.TButton",
            foreground=text_primary,
            padding=(12, 8),
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 10),
            background="#1f3a52",
        )
        style.map(
            "Action.TButton",
            background=[("active", "#2b5276"), ("pressed", "#244666")],
            foreground=[("disabled", "#4c5f75")],
        )
        style.configure(
            "Primary.TButton",
            padding=(18, 12),
            borderwidth=0,
            relief="flat",
            font=("Segoe UI Semibold", 11),
            background=accent,
            foreground="#0a111b",
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#7fd1ff"), ("pressed", "#55addd")],
            foreground=[("disabled", "#4a5b6f")],
        )
        style.configure(
            "Search.TEntry",
            fieldbackground="#142538",
            foreground=text_primary,
            bordercolor="#275072",
            insertcolor=accent,
            padding=(12, 8),
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "Search.TEntry",
            fieldbackground=[("focus", "#1c3a57")],
            bordercolor=[("focus", "#5aa6f0")],
        )
        style.configure(
            "Path.TEntry",
            fieldbackground="#142538",
            foreground=text_primary,
            bordercolor="#2d4b6e",
            insertcolor=accent,
            padding=(12, 8),
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "Path.TEntry",
            fieldbackground=[("focus", "#1c3a57")],
            bordercolor=[("focus", "#5aa6f0")],
        )
        style.configure(
            "Treeview",
            background="#0f1928",
            foreground=text_primary,
            fieldbackground="#0f1928",
            bordercolor="#22364b",
             borderwidth=0,
            rowheight=26,
            font=("Segoe UI", 10),
        )
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map(
            "Treeview",
            background=[("selected", "#4ba8ff")],
            foreground=[("selected", "#09131f")],
        )
        style.configure(
            "Treeview.Heading",
            background="#1d2f45",
            foreground="#9ed6ff",
            relief="flat",
            font=("Segoe UI Semibold", 10),
            padding=(12, 6, 12, 6),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#25425e")],
        )

    def build_gui(self):
        self.surface = tk.Frame(self.background_canvas, bg="#152131", highlightthickness=0)
        self.canvas_window = self.background_canvas.create_window(0, 0, anchor="nw", window=self.surface)
        card = tk.Frame(self.surface, bg="#152131", highlightthickness=1, highlightbackground="#223753")
        card.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        self.surface.grid_rowconfigure(0, weight=1)
        self.surface.grid_columnconfigure(0, weight=1)
        main = ttk.Frame(card, padding=24, style="Surface.TFrame")
        main.grid(row=0, column=0, sticky="nsew")
        card.grid_rowconfigure(0, weight=1)
        card.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1, uniform="col")
        main.grid_columnconfigure(1, weight=1, uniform="col")
        main.grid_rowconfigure(1, weight=1)
        header = ttk.Frame(main, style="Surface.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        ttk.Label(header, text="Steam Depot GUI", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(header, textvariable=self.download_log_var, style="Status.TLabel").grid(row=1, column=1, sticky="e", pady=(2, 0))
        left_container = ttk.Frame(main, style="Surface.TFrame")
        left_container.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        left_container.grid_rowconfigure(1, weight=1)
        left_container.grid_columnconfigure(0, weight=1)
        ttk.Label(left_container, text="Added Games", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        left_card = tk.Frame(left_container, bg="#182c44", highlightthickness=1, highlightbackground="#223753")
        left_card.grid(row=1, column=0, sticky="nsew")
        left_card.grid_rowconfigure(0, weight=1)
        left_card.grid_columnconfigure(0, weight=1)
        left = ttk.Frame(left_card, padding=16, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        tree_frame_added = ttk.Frame(left, style="Panel.TFrame")
        tree_frame_added.grid(row=0, column=0, sticky="nsew")
        self.added_tree = ttk.Treeview(tree_frame_added, columns=("name", "appid", "repo"), show="headings", selectmode="browse")
        self.added_tree["displaycolumns"] = ("name", "appid", "repo")
        self.added_tree.heading("name", text="Name", anchor=tk.W)
        self.added_tree.heading("appid", text="App ID", anchor=tk.CENTER)
        self.added_tree.heading("repo", text="Repository", anchor=tk.W)
        self.added_tree.column("name", anchor=tk.W, width=280, minwidth=200, stretch=True)
        self.added_tree.column("appid", anchor=tk.CENTER, width=90, minwidth=90, stretch=False)
        self.added_tree.column("repo", anchor=tk.W, width=140, minwidth=120, stretch=False)
        self.added_tree.grid(row=0, column=0, sticky="nsew")
        self.added_scroll = ttk.Scrollbar(tree_frame_added, orient="vertical", command=self.added_tree.yview)
        self.added_scroll.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        tree_frame_added.grid_rowconfigure(0, weight=1)
        tree_frame_added.grid_columnconfigure(0, weight=1)
        self.added_tree.configure(yscrollcommand=self.added_scroll.set)
        self.apply_tree_tags(self.added_tree)
        controls = ttk.Frame(left, style="Panel.TFrame")
        controls.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        controls.grid_columnconfigure(0, weight=1)
        controls.grid_columnconfigure(1, weight=1)
        self.remove_button = RoundedButton(
            controls,
            text="Remove Selected",
            command=self.remove_selected,
            fill="#1f3a52",
            hover="#2e567a",
            active="#244666",
            text_color="#dee8f6",
            font=("Segoe UI Semibold", 10),
            background="#182c44",
        )
        self.remove_button.grid(row=0, column=0, sticky="w")
        self.restart_button = RoundedButton(
            controls,
            text="Restart Steam",
            command=self.restart_steam,
            fill="#66c0f4",
            hover="#7fd1ff",
            active="#55addd",
            text_color="#0a111b",
            font=("Segoe UI Semibold", 10),
            background="#182c44",
        )
        self.restart_button.grid(row=0, column=1, sticky="e")
        steam_path_frame = ttk.Frame(left, style="Panel.TFrame")
        steam_path_frame.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        steam_path_frame.grid_columnconfigure(1, weight=1)
        ttk.Label(steam_path_frame, text="Steam Folder:", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(steam_path_frame, textvariable=self.steam_folder_var, style="Path.TEntry")
        entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.steam_folder_button = RoundedButton(
            steam_path_frame,
            text="Browse",
            command=self.choose_steam_folder,
            fill="#1f3a52",
            hover="#2e567a",
            active="#244666",
            text_color="#dee8f6",
            font=("Segoe UI Semibold", 10),
            width=110,
            background="#182c44",
        )
        self.steam_folder_button.grid(row=0, column=2, sticky="e", padx=(12, 0))
        right_container = ttk.Frame(main, style="Surface.TFrame")
        right_container.grid(row=1, column=1, sticky="nsew", padx=(12, 0))
        right_container.grid_rowconfigure(1, weight=1)
        right_container.grid_columnconfigure(0, weight=1)
        ttk.Label(right_container, text="Steam Search", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        right_card = tk.Frame(right_container, bg="#182c44", highlightthickness=1, highlightbackground="#223753")
        right_card.grid(row=1, column=0, sticky="nsew")
        right_card.grid_rowconfigure(0, weight=1)
        right_card.grid_columnconfigure(0, weight=1)
        right = ttk.Frame(right_card, padding=16, style="Panel.TFrame")
        right.grid(row=0, column=0, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        search_entry = ttk.Entry(right, textvariable=self.search_var, style="Search.TEntry")
        search_entry.grid(row=0, column=0, sticky="ew", pady=(4, 12))
        search_entry.bind("<KeyRelease>", self.on_search_change)
        tree_frame_results = ttk.Frame(right, style="Panel.TFrame")
        tree_frame_results.grid(row=1, column=0, sticky="nsew")
        tree_frame_results.grid_columnconfigure(0, weight=1)
        tree_frame_results.grid_rowconfigure(0, weight=1)
        self.results_tree = ttk.Treeview(tree_frame_results, columns=("name", "appid", "repo"), show="headings", selectmode="browse")
        self.results_tree["displaycolumns"] = ("name", "appid", "repo")
        self.results_tree.heading("name", text="Name", anchor=tk.W)
        self.results_tree.heading("appid", text="App ID", anchor=tk.CENTER)
        self.results_tree.heading("repo", text="Repository", anchor=tk.W)
        self.results_tree.column("name", anchor=tk.W, width=260, minwidth=200, stretch=True)
        self.results_tree.column("appid", anchor=tk.CENTER, width=90, minwidth=90, stretch=False)
        self.results_tree.column("repo", anchor=tk.W, width=160, minwidth=140, stretch=True)
        self.results_tree.grid(row=0, column=0, sticky="nsew")
        self.result_scroll = ttk.Scrollbar(tree_frame_results, orient="vertical", command=self.results_tree.yview)
        self.result_scroll.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.results_tree.configure(yscrollcommand=self.result_scroll.set)
        self.results_tree.bind("<Double-1>", self.on_add_double_click)
        self.apply_tree_tags(self.results_tree)
        add_frame = ttk.Frame(right, style="Panel.TFrame")
        add_frame.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        add_frame.grid_columnconfigure(0, weight=1)
        self.add_button = RoundedButton(
            add_frame,
            text="Add Selected",
            command=self.add_selected,
            fill="#66c0f4",
            hover="#7fd1ff",
            active="#55addd",
            text_color="#0a111b",
            font=("Segoe UI Semibold", 10),
            background="#182c44",
        )
        self.add_button.grid(row=0, column=0, sticky="e")
        self.root.update_idletasks()
        self.on_resize()
        required_width = max(self.surface.winfo_reqwidth() + 48, 1080)
        required_height = max(self.surface.winfo_reqheight() + 48, 640)
        self.root.geometry(f"{required_width}x{required_height}")
        self.root.minsize(required_width, required_height)

    def apply_tree_tags(self, tree):
        tree.tag_configure("even", background="#142233", foreground="#d6e6ff")
        tree.tag_configure("odd", background="#122030", foreground="#c9dbf8")
        tree.tag_configure("header", font=("Segoe UI Semibold", 10))

    def choose_steam_folder(self):
        folder = filedialog.askdirectory(title="Select Steam Folder", initialdir=self.steam_folder_var.get())
        if folder:
            self.steam_folder_var.set(folder)
            self.integrator = GreenLumaIntegrator(folder)
            messagebox.showinfo("Steam Folder", "Steam folder updated.", parent=self.root)

    def on_search_change(self, event=None):
        if self.search_after_id:
            self.root.after_cancel(self.search_after_id)
        self.search_after_id = self.root.after(SEARCH_DELAY_MS, self.start_search)

    def start_search(self):
        query = self.search_var.get().strip()
        if not query:
            self.clear_results()
            self.status_var.set(STATUS_IDLE)
            return
        if self.active_search:
            self.active_search = None
        self.status_var.set("Searching...")
        thread = threading.Thread(target=self.search_worker, args=(query,), daemon=True)
        self.active_search = thread
        thread.start()

    def search_worker(self, query):
        try:
            logger.info("Searching for apps matching '%s'", query)
            url = f"https://steamcommunity.com/actions/SearchApps/{urllib.parse.quote(query)}"
            data = request_json(url)
            results = []
            for entry in data:
                appid = str(entry.get("appid", "")).strip()
                name = entry.get("name", "").strip()
                if not appid or not name:
                    continue
                repos = []
                for repo_id, repo in self.repo_objects.items():
                    try:
                        logger.info("Checking repo %s for app id %s", repo_id, appid)
                        if not repo.branch_exists(appid):
                            logger.info("Repo %s has no branch for app id %s", repo_id, appid)
                            continue
                        paths = repo.fetch_manifest_paths(appid)
                    except Exception:
                        logger.exception("Failed to inspect repo %s for app %s", repo_id, appid)
                        continue
                    if paths:
                        repos.append({"repo": repo_id, "branch": appid, "paths": paths})
                        logger.info(
                            "Found manifest paths for app %s in repo %s: %s",
                            appid,
                            repo_id,
                            ", ".join(paths),
                        )
                if repos:
                    results.append({"appid": appid, "name": name, "repos": repos})
                    logger.info(
                        "Found app id %s (%s) with repos: %s",
                        appid,
                        name,
                        ", ".join(repo_entry["repo"] for repo_entry in repos),
                    )
                else:
                    logger.info("No manifests found for app %s (%s)", appid, name)
            self.queue.put(("search_results", query, results))
        except Exception as exc:
            logger.exception("Search failed for query '%s'", query)
            self.queue.put(("search_error", str(exc)))

    def clear_results(self):
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.latest_results.clear()

    def add_selected(self):
        selection = self.results_tree.selection()
        if not selection:
            return
        iid = selection[0]
        data = self.latest_results.get(iid)
        if not data:
            return
        self.download_game(data)

    def on_add_double_click(self, event):
        self.add_selected()

    def download_game(self, data):
        repo_id = data["repo"]
        branch = data["branch"]
        paths = data.get("paths")
        repo = self.repo_objects.get(repo_id)
        if not repo:
            messagebox.showerror("Repository Missing", "Repository not configured.", parent=self.root)
            return
        steam_path = Path(self.steam_folder_var.get())
        if not steam_path.exists():
            messagebox.showerror("Steam Folder", "Steam folder not found.", parent=self.root)
            return
        self.integrator = GreenLumaIntegrator(steam_path)
        appid = data["appid"]
        name = data["name"]
        self.status_var.set(f"Downloading {name} ({appid})...")
        self.set_download_log(f"Preparing {appid}")
        logger.info("Queued download for %s (%s) from %s on branch %s", name, appid, repo_id, branch)
        thread = threading.Thread(target=self.download_worker, args=(repo, repo_id, branch, appid, name, paths), daemon=True)
        thread.start()

    def download_worker(self, repo, repo_id, branch, appid, name, paths):
        temp_dir = Path.cwd() / "_temp_downloads"
        temp_dir.mkdir(exist_ok=True)
        logger.info("Starting download job for %s (%s) from %s (branch %s)", name, appid, repo_id, branch)
        try:
            if not paths:
                logger.info("No manifest list provided; fetching manifest paths for %s", appid)
                paths = repo.fetch_manifest_paths(branch)
                if not paths:
                    raise RuntimeError("No manifest files available for this title.")
            lua_paths = [path for path in paths if path.lower().endswith(".lua")]
            key_paths = [path for path in paths if path.lower().endswith(".key")]
            if not lua_paths:
                raise RuntimeError("Repository did not provide a Lua configuration file.")
            wanted_paths = lua_paths + key_paths
            self.set_download_log(f"Downloading {appid} Lua")
            files = repo.download_files(branch, wanted_paths, temp_dir)
            logger.info(
                "Downloaded %d Lua/Key file(s) for %s (%s): %s",
                len(files),
                name,
                appid,
                ", ".join(str(path) for path in files),
            )
            lua_files = [path for path in files if path.suffix.lower() == ".lua"]
            if not lua_files:
                raise RuntimeError("The repository does not provide a Lua configuration file for this title.")

            lua_depots = set()
            for lua_path in lua_files:
                content = lua_path.read_text(encoding="utf-8", errors="ignore")
                for depot_id in iter_lua_manifest_depot_ids(content):
                    lua_depots.add(depot_id)
            if not lua_depots:
                raise RuntimeError("No depot entries found via setManifestId in the Lua configuration file.")
            logger.info(
                "Lua configuration references depots via setManifestId: %s",
                ", ".join(sorted(lua_depots)),
            )

            existing_manifest_paths = [path for path in files if path.suffix.lower() == ".manifest"]
            if existing_manifest_paths:
                logger.info(
                    "Discarding %d manifest file(s) bundled with the repository in favour of live Steam data.",
                    len(existing_manifest_paths),
                )
                for old_manifest in existing_manifest_paths:
                    try:
                        old_manifest.unlink()
                    except Exception:
                        pass
                files = [path for path in files if path.suffix.lower() != ".manifest"]

            self.set_download_log("Getting latest depots")
            windows_manifests = fetch_latest_windows_manifests(appid)
            if not windows_manifests:
                raise RuntimeError("No Windows depots with visible manifests are available for this app.")
            self.set_download_log("Getting latest manifests")

            windows_depot_ids = {entry["depot_id"] for entry in windows_manifests}
            extra_windows = sorted(windows_depot_ids - lua_depots)
            if extra_windows:
                logger.info(
                    "Ignoring Windows depot(s) not referenced in Lua: %s",
                    ", ".join(extra_windows),
                )
            missing_windows = sorted(lua_depots - windows_depot_ids)
            if missing_windows:
                logger.info(
                    "Lua configuration references depot(s) without Windows manifests: %s",
                    ", ".join(missing_windows),
                )
            windows_manifests = [
                entry for entry in windows_manifests if entry["depot_id"] in lua_depots
            ]
            if not windows_manifests:
                raise RuntimeError(
                    "None of the depots referenced in the Lua configuration have Windows manifests available."
                )

            self.set_download_log("Downloading latest manifests")
            manifest_paths = []
            seen_pairs = set()
            allowed_depots = set()
            for entry in windows_manifests:
                depot_id = entry["depot_id"]
                manifest_id = entry["manifest_id"]
                pair = (depot_id, manifest_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                allowed_depots.add(depot_id)
                manifest_path = download_remote_manifest(depot_id, manifest_id, temp_dir)
                manifest_paths.append(manifest_path)

            files.extend(manifest_paths)
            logger.info(
                "Downloaded %d manifest(s) for Windows depots referenced in Lua: %s",
                len(manifest_paths),
                ", ".join(path.name for path in manifest_paths),
            )

            manifest_files, depot_keys, applist_files, rename_map = self.integrator.apply(
                appid,
                name,
                files,
                allowed_depots=allowed_depots,
                progress_callback=self.set_download_log,
            )
            if rename_map:
                self.store.update_applist_files(rename_map)
            self.store.add(repo_id, appid, name, manifest_files, depot_keys, applist_files)
            self.set_download_log("Cleaning up")
            for file in files:
                try:
                    file.unlink()
                    logger.info("Deleted temporary file %s", file)
                except Exception:
                    pass
            self.queue.put(("download_complete", True, repo_id, appid, name))
        except Exception as exc:
            logger.exception("Download failed for %s (%s)", name, appid)
            self.set_download_log("Download failed")
            self.queue.put(("download_complete", False, str(exc)))
        finally:
            leftovers = list(temp_dir.iterdir())
            if not leftovers:
                try:
                    temp_dir.rmdir()
                    logger.info("Removed temporary download directory %s", temp_dir)
                except Exception:
                    pass
            else:
                logger.info(
                    "Temporary directory %s retained with leftover files: %s",
                    temp_dir,
                    ", ".join(str(item) for item in leftovers),
                )

    def remove_selected(self):
        selection = self.added_tree.selection()
        if not selection:
            return
        iid = selection[0]
        repo_id, appid = iid.split(":", 1)
        info = self.store.get(repo_id, appid)
        if not info:
            return
        name = info.get("name", f"App {appid}")
        if not messagebox.askyesno("Remove Game", f"Remove {name}?", parent=self.root):
            return
        depot_keys = info.get("depot_keys", {})
        manifest_files = info.get("files", [])
        rename_map = self.integrator.remove_entry(appid, depot_keys, manifest_files)
        if rename_map:
            self.store.update_applist_files(rename_map)
        self.store.remove(repo_id, appid)
        self.refresh_added()
        self.status_var.set(f"{name} removed.")

    def restart_steam(self):
        steam_root = Path(self.steam_folder_var.get())
        if not steam_root.exists():
            messagebox.showerror("Steam", "Steam folder not found.", parent=self.root)
            return
        dll_injector = steam_root / "DLLInjector.exe"
        if not dll_injector.exists():
            messagebox.showerror("Steam", "DLLInjector.exe not found in the Steam folder.", parent=self.root)
            return
        self.status_var.set("Applying DLL injector...")
        logger.info("Preparing to launch DLLInjector at %s", dll_injector)
        try:
            if os.name == "nt":
                import subprocess

                subprocess.run(["taskkill", "/IM", "steam.exe", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
                logger.info("Launching DLLInjector.exe")
                subprocess.Popen([str(dll_injector)], cwd=str(steam_root))
            else:
                import subprocess

                subprocess.run(["pkill", "steam"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
                subprocess.Popen([str(dll_injector)], cwd=str(steam_root))
            self.status_var.set("DLL injector launched.")
        except Exception as exc:
            logger.exception("Failed to launch DLLInjector")
            self.status_var.set(STATUS_IDLE)
            messagebox.showerror("Steam", f"Unable to launch DLLInjector.exe: {exc}", parent=self.root)

    def refresh_added(self):
        for item in self.added_tree.get_children():
            self.added_tree.delete(item)
        entries = []
        for repo, appid, name, info in self.store.list_games():
            entries.append((name.lower(), repo, appid, name))
        for index, (_, repo, appid, name) in enumerate(sorted(entries)):
            iid = f"{repo}:{appid}"
            repo_display = repo.split("/", 1)[-1]
            tags = ("even",) if index % 2 == 0 else ("odd",)
            self.added_tree.insert("", "end", iid=iid, values=(name, appid, repo_display), tags=tags)

    def process_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "search_results":
                    _, query, results = item
                    if query != self.search_var.get().strip():
                        continue
                    self.populate_results(results)
                elif kind == "search_error":
                    _, message = item
                    self.status_var.set(STATUS_IDLE)
                    messagebox.showerror("Search Failed", message, parent=self.root)
                elif kind == "download_complete":
                    if item[1]:
                        _, _, repo_id, appid, name = item
                        self.refresh_added()
                        self.status_var.set(f"{name} added.")
                        self.set_download_log(f"Finished {appid}")
                    else:
                        _, _, message = item
                        self.status_var.set(STATUS_IDLE)
                        self.set_download_log("Download failed")
                        messagebox.showerror("Download Failed", message, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(150, self.process_queue)

    def set_download_log(self, message):
        logger.info("Progress: %s", message)
        def update():
            self.download_log_var.set(message)
        if threading.current_thread() is threading.main_thread():
            update()
        else:
            self.root.after(0, update)

    def populate_results(self, results):
        self.clear_results()
        row_index = 0
        for entry in results:
            appid = entry["appid"]
            name = entry["name"]
            for repo_entry in entry["repos"]:
                repo_id = repo_entry["repo"]
                repo_display = repo_id.split("/", 1)[-1]
                iid = f"{repo_id}:{appid}"
                tags = ("even",) if row_index % 2 == 0 else ("odd",)
                self.results_tree.insert("", "end", iid=iid, values=(name, appid, repo_display), tags=tags)
                self.latest_results[iid] = {
                    "appid": appid,
                    "name": name,
                    "repo": repo_id,
                    "branch": repo_entry["branch"],
                    "paths": repo_entry.get("paths"),
                }
                row_index += 1
        if results:
            self.status_var.set(f"{sum(len(x['repos']) for x in results)} result(s)")
        else:
            self.status_var.set("No results found.")

    def run(self):
        self.root.mainloop()


def main():
    app = SteamDepotApp()
    app.run()


if __name__ == "__main__":
    main()
