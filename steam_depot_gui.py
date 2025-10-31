import atexit
import logging
import json
import zipfile
import math
import os
import queue
import re
import shutil
import sqlite3
import ctypes
from ctypes import wintypes
import sys
import tempfile
import threading
import time
import unicodedata
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
STORE_APP_DETAILS_URL_TEMPLATE = "https://store.steampowered.com/api/appdetails?appids={appid}&l={language}"
STEAM_APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2"
STEAMSPY_ALL_URL = "https://steamspy.com/api.php?request=all"
TTL_STEAM_SEARCH = 7 * 24 * 3600
TTL_STEAMSPY = 24 * 3600
STEAM_SEARCH_LIMIT = 5


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


def resolve_asset_path(filename):
    candidates = []
    asset_subpaths = [filename, f"assets/{filename}", f"resources/{filename}"]
    try:
        package_dir = Path(__file__).resolve().parent
        for subpath in asset_subpaths:
            candidates.append(package_dir / subpath)
    except Exception:
        pass
    for subpath in asset_subpaths:
        candidates.append(Path.cwd() / subpath)
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        for subpath in asset_subpaths:
            candidates.append(Path(bundle_dir) / subpath)
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists():
            return resolved
    return None


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


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, guid_str=None):
        super().__init__()
        if guid_str:
            parts = guid_str.strip("{}").split("-")
            self.Data1 = int(parts[0], 16)
            self.Data2 = int(parts[1], 16)
            self.Data3 = int(parts[2], 16)
            data4 = bytes.fromhex(parts[3] + parts[4])
            for idx in range(8):
                self.Data4[idx] = data4[idx]


def guid_equal(lhs, rhs):
    return (
        lhs.Data1 == rhs.Data1
        and lhs.Data2 == rhs.Data2
        and lhs.Data3 == rhs.Data3
        and bytes(lhs.Data4) == bytes(rhs.Data4)
    )


class FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", wintypes.UINT),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", wintypes.DWORD),
        ("lindex", ctypes.c_long),
        ("tymed", wintypes.DWORD),
    ]


class STGMEDIUM_UNION(ctypes.Union):
    _fields_ = [
        ("hGlobal", wintypes.HGLOBAL),
        ("pstm", ctypes.c_void_p),
        ("pstg", ctypes.c_void_p),
    ]


class STGMEDIUM(ctypes.Structure):
    _fields_ = [
        ("tymed", wintypes.DWORD),
        ("union", STGMEDIUM_UNION),
        ("pUnkForRelease", ctypes.c_void_p),
    ]


HRESULT = wintypes.LONG
DROPEFFECT_NONE = wintypes.DWORD(0)
DROPEFFECT_COPY = wintypes.DWORD(1)
CF_HDROP = 15
TYMED_HGLOBAL = 1
DVASPECT_CONTENT = 1
E_NOINTERFACE = 0x80004002


class IDataObjectVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.c_void_p),
        ("AddRef", ctypes.c_void_p),
        ("Release", ctypes.c_void_p),
        ("GetData", ctypes.c_void_p),
        ("GetDataHere", ctypes.c_void_p),
        ("QueryGetData", ctypes.c_void_p),
        ("GetCanonicalFormatEtc", ctypes.c_void_p),
        ("SetData", ctypes.c_void_p),
        ("EnumFormatEtc", ctypes.c_void_p),
        ("DAdvise", ctypes.c_void_p),
        ("DUnadvise", ctypes.c_void_p),
        ("EnumDAdvise", ctypes.c_void_p),
    ]


class IDataObject(ctypes.Structure):
    _fields_ = [
        ("lpVtbl", ctypes.POINTER(IDataObjectVtbl)),
    ]


class IDropTargetVtbl(ctypes.Structure):
    pass


class IDropTarget(ctypes.Structure):
    _fields_ = [
        ("lpVtbl", ctypes.POINTER(IDropTargetVtbl)),
    ]


class DropTargetStruct(ctypes.Structure):
    _fields_ = [
        ("lpVtbl", ctypes.POINTER(IDropTargetVtbl)),
        ("ref_count", ctypes.c_ulong),
        ("py_object", ctypes.py_object),
    ]


class WindowsZipDropTarget:
    IID_IUNKNOWN = GUID("00000000-0000-0000-C000-000000000046")
    IID_IDROPTARGET = GUID("00000122-0000-0000-C000-000000000046")

    def __init__(self, widget, event_queue):
        self.widget = widget
        self.queue = event_queue
        self.shell32 = ctypes.windll.shell32
        self.shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        self.shell32.DragQueryFileW.restype = wintypes.UINT
        self.shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.shell32.DragAcceptFiles.restype = None
        self.ole32 = ctypes.windll.ole32
        self.ole32.ReleaseStgMedium.argtypes = [ctypes.POINTER(STGMEDIUM)]
        self.ole32.ReleaseStgMedium.restype = None
        self._ole_initialized = False
        self._register_ole()
        self._create_vtable()
        self._struct = DropTargetStruct(
            ctypes.pointer(self._vtbl),
            1,
            ctypes.py_object(self),
        )
        self._drop_target = ctypes.pointer(self._struct)
        hwnd = widget.winfo_id()
        hr = self.ole32.RegisterDragDrop(wintypes.HWND(hwnd), ctypes.cast(self._drop_target, ctypes.POINTER(IDropTarget)))
        if hr != 0:
            raise ctypes.WinError(hr)
        self.shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
        self._current_files = []
        self._has_zip = False

    def _register_ole(self):
        hr = self.ole32.OleInitialize(None)
        if hr in (0, 1):  # S_OK or S_FALSE
            self._ole_initialized = True

    def _create_vtable(self):
        QueryInterfaceProto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        AddRefProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        DragEnterProto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
        DragOverProto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
        DragLeaveProto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
        DropProto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)

        def get_self(this_ptr):
            struct = ctypes.cast(this_ptr, ctypes.POINTER(DropTargetStruct)).contents
            return struct.py_object

        def qi(this_ptr, riid_ptr, out_ptr):
            self_obj = get_self(this_ptr)
            if guid_equal(riid_ptr.contents, self_obj.IID_IUNKNOWN) or guid_equal(riid_ptr.contents, self_obj.IID_IDROPTARGET):
                out_ptr[0] = ctypes.cast(this_ptr, ctypes.c_void_p)
                self_obj._add_ref()
                return 0
            out_ptr[0] = ctypes.c_void_p()
            return HRESULT(E_NOINTERFACE)

        def add_ref(this_ptr):
            struct = ctypes.cast(this_ptr, ctypes.POINTER(DropTargetStruct)).contents
            struct.ref_count += 1
            return struct.ref_count

        def release(this_ptr):
            struct = ctypes.cast(this_ptr, ctypes.POINTER(DropTargetStruct)).contents
            if struct.ref_count > 0:
                struct.ref_count -= 1
            return struct.ref_count

        def drag_enter(this_ptr, data_obj_ptr, key_state, point, effect_ptr):
            self_obj = get_self(this_ptr)
            files = self_obj._extract_files(data_obj_ptr)
            self_obj._current_files = files
            self_obj._has_zip = any(path.lower().endswith(".zip") for path in files)
            self_obj._post_event(("drop_hover", self_obj._has_zip))
            effect_ptr[0] = DROPEFFECT_COPY if self_obj._has_zip else DROPEFFECT_NONE
            return 0

        def drag_over(this_ptr, key_state, point, effect_ptr):
            self_obj = get_self(this_ptr)
            effect_ptr[0] = DROPEFFECT_COPY if self_obj._has_zip else DROPEFFECT_NONE
            return 0

        def drag_leave(this_ptr):
            self_obj = get_self(this_ptr)
            self_obj._current_files = []
            self_obj._has_zip = False
            self_obj._post_event(("drop_hover", False))
            return 0

        def drop(this_ptr, data_obj_ptr, key_state, point):
            self_obj = get_self(this_ptr)
            files = self_obj._extract_files(data_obj_ptr)
            zip_files = [str(path) for path in files if path.lower().endswith(".zip")]
            self_obj._current_files = []
            self_obj._has_zip = False
            self_obj._post_event(("drop_hover", False))
            if zip_files:
                self_obj._post_event(("drop_zip", zip_files[0]))
            else:
                self_obj._post_event(("drop_zip", None))
            return 0

        self._qi = QueryInterfaceProto(qi)
        self._addref = AddRefProto(add_ref)
        self._release = ReleaseProto(release)
        self._drag_enter = DragEnterProto(drag_enter)
        self._drag_over = DragOverProto(drag_over)
        self._drag_leave = DragLeaveProto(drag_leave)
        self._drop = DropProto(drop)

        IDropTargetVtbl._fields_ = [
            ("QueryInterface", QueryInterfaceProto),
            ("AddRef", AddRefProto),
            ("Release", ReleaseProto),
            ("DragEnter", DragEnterProto),
            ("DragOver", DragOverProto),
            ("DragLeave", DragLeaveProto),
            ("Drop", DropProto),
        ]
        self._vtbl = IDropTargetVtbl(
            self._qi,
            self._addref,
            self._release,
            self._drag_enter,
            self._drag_over,
            self._drag_leave,
            self._drop,
        )

    def _post_event(self, payload):
        try:
            self.queue.put_nowait(payload)
        except Exception:
            logger.exception("Failed to post drag-and-drop event")

    def _add_ref(self):
        struct = self._struct
        struct.ref_count += 1
        return struct.ref_count

    def _extract_files(self, data_obj_ptr):
        if not data_obj_ptr:
            return []
        data_obj = ctypes.cast(data_obj_ptr, ctypes.POINTER(IDataObject))
        get_data_func = ctypes.cast(data_obj.contents.lpVtbl.contents.GetData, ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM)))
        fmt = FORMATETC()
        fmt.cfFormat = CF_HDROP
        fmt.ptd = None
        fmt.dwAspect = DVASPECT_CONTENT
        fmt.lindex = -1
        fmt.tymed = TYMED_HGLOBAL
        medium = STGMEDIUM()
        hr = get_data_func(data_obj_ptr, ctypes.byref(fmt), ctypes.byref(medium))
        if hr != 0:
            return []
        try:
            hdrop = medium.union.hGlobal
            if not hdrop:
                return []
            handle = wintypes.HANDLE(hdrop)
            results = []
            count = self.shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            for index in range(count):
                length = self.shell32.DragQueryFileW(handle, index, None, 0) + 1
                buffer = ctypes.create_unicode_buffer(length)
                self.shell32.DragQueryFileW(handle, index, buffer, length)
                results.append(buffer.value)
            return results
        finally:
            self.ole32.ReleaseStgMedium(ctypes.byref(medium))

    def unregister(self):
        try:
            hwnd = self.widget.winfo_id()
            self.ole32.RevokeDragDrop(wintypes.HWND(hwnd))
            self.shell32.DragAcceptFiles(wintypes.HWND(hwnd), False)
        except Exception:
            logger.exception("Failed to revoke drag-and-drop")
        if self._ole_initialized:
            self.ole32.OleUninitialize()
            self._ole_initialized = False



class SteamSearchManager:
    def __init__(self, limit=STEAM_SEARCH_LIMIT, use_popularity=True, ephemeral=True, status_callback=None):
        self.limit = max(1, int(limit))
        self.use_popularity = use_popularity
        self.ephemeral = ephemeral
        self.status_callback = status_callback
        self.cache_dir = None
        self.db_path = None
        self.ready_event = threading.Event()
        self.init_error = None
        self._thread = None
        self._lock = threading.Lock()
        self._atexit_handler = None
        self._closed = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._initialize, name="SteamSearchInit", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout=None):
        self.ready_event.wait(timeout)

    def search(self, query):
        if not query:
            return []
        self.wait_ready()
        if self.init_error:
            raise self.init_error
        if not self.db_path:
            return []
        return self._search_db(self.db_path, query, self.limit, self.use_popularity)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.wait_ready()
        if self._atexit_handler:
            try:
                atexit.unregister(self._atexit_handler)
            except Exception:
                pass
            self._atexit_handler = None
        if self.cache_dir:
            self._delete_cache_dir(self.cache_dir)

    def _initialize(self):
        self._emit_status("Getting search ready...")
        try:
            self.cache_dir = self._cache_dir(self.ephemeral)
            self.db_path = os.path.join(self.cache_dir, "steam.sqlite")
            self._register_cleanup()
            self._prewarm()
            if not self._closed:
                self._emit_status("Search ready")
        except Exception as exc:
            self.init_error = exc
            logger.exception("Failed to initialize Steam search cache")
            self._emit_status("Search unavailable")
        finally:
            self.ready_event.set()

    def _emit_status(self, message):
        if self.status_callback:
            try:
                self.status_callback(message)
            except Exception:
                logger.exception("Search status callback failed")

    def _register_cleanup(self):
        if not self.ephemeral or not self.cache_dir:
            return

        def _cleanup():
            self._delete_cache_dir(self.cache_dir)

        self._atexit_handler = _cleanup
        atexit.register(_cleanup)

    def _prewarm(self):
        if not self._fts5_ok():
            raise RuntimeError("SQLite FTS5 is required for Steam search but is unavailable in this Python build.")
        needs_build = (not os.path.exists(self.db_path)) or self._db_age(self.db_path) > TTL_STEAM_SEARCH
        needs_pop = needs_build or self._db_age(self.db_path) > TTL_STEAMSPY
        self._rebuild_db(self.db_path, force=needs_build, refresh_popularity=needs_pop)

    def _cache_dir(self, persistent):
        if not persistent:
            path = tempfile.mkdtemp(prefix="steam_search_")
            logger.info("Steam search using ephemeral cache at %s", path)
            return path
        base = os.path.join(os.path.expanduser("~"), ".cache", "steam_search_fast")
        if os.name == "nt":
            base = os.path.join(os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "steam_search_fast")
        os.makedirs(base, exist_ok=True)
        logger.info("Steam search using persistent cache at %s", base)
        return base

    def _delete_cache_dir(self, path):
        if not path:
            return
        if not os.path.exists(path):
            logger.info("Steam search cache already removed: %s", path)
            return
        last_err = None
        for attempt in range(1, 6):
            try:
                shutil.rmtree(path)
                logger.info("Steam search cache deleted: %s", path)
                return
            except Exception as exc:
                last_err = exc
                logger.warning("Attempt %s to delete Steam search cache failed: %s", attempt, exc)
                time.sleep(0.25 * attempt)
        logger.error("Unable to delete Steam search cache at %s after retries: %s", path, last_err)

    @staticmethod
    def _fts5_ok():
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    @staticmethod
    def _db_age(path):
        if not os.path.exists(path):
            return float("inf")
        return time.time() - os.path.getmtime(path)

    def _rebuild_db(self, db_path, force=False, refresh_popularity=False):
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=OFF;")
            conn.execute("PRAGMA synchronous=OFF;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA page_size=32768;")
            conn.execute("PRAGMA cache_size=200000;")
            conn.execute("PRAGMA mmap_size=268435456;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS apps(
                    appid INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_norm TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS apps_fts USING fts5(
                    name_norm, content='apps', content_rowid='appid',
                    tokenize='unicode61', prefix='2 3 4 5 6'
                );
                CREATE TABLE IF NOT EXISTS pop(
                    appid INTEGER PRIMARY KEY,
                    owners_mid INTEGER DEFAULT 0,
                    ccu INTEGER DEFAULT 0,
                    userscore INTEGER DEFAULT 0,
                    avg2w INTEGER DEFAULT 0
                );
                """
            )
            if force:
                logger.info("Downloading Steam app list for search index")
                apps = self._fetch_applist()
                logger.info("Building Steam search index with %s apps", len(apps))
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute("DELETE FROM apps;")
                conn.execute("DELETE FROM apps_fts;")
                conn.executemany("INSERT INTO apps(appid,name,name_norm) VALUES(?,?,?)", apps)
                conn.execute("INSERT INTO apps_fts(rowid,name_norm) SELECT appid,name_norm FROM apps;")
                conn.execute("COMMIT;")
            if refresh_popularity:
                logger.info("Refreshing Steam popularity metrics for search ordering")
                rows = self._fetch_steamspy_all()
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute("DELETE FROM pop;")
                conn.executemany(
                    "INSERT OR REPLACE INTO pop(appid,owners_mid,ccu,userscore,avg2w) VALUES(?,?,?,?,?)",
                    rows,
                )
                conn.execute("COMMIT;")
            conn.execute("PRAGMA optimize;")
        finally:
            conn.close()

    @staticmethod
    def _strip_accents(value):
        return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))

    def _normalize(self, text):
        text = self._strip_accents(text or "").lower()
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _fetch_applist(self):
        payload = request_json(STEAM_APPLIST_URL)
        apps = payload.get("applist", {}).get("apps", [])
        result = []
        for entry in apps:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            appid = entry.get("appid")
            try:
                app_id_int = int(appid)
            except (TypeError, ValueError):
                continue
            result.append((app_id_int, name, self._normalize(name)))
        return result

    @staticmethod
    def _parse_owners_mid(raw):
        try:
            lo, hi = [int(part.strip().replace(",", "")) for part in raw.split("..")]
            return (lo + hi) // 2
        except Exception:
            return 0

    def _fetch_steamspy_all(self):
        data = request_json(STEAMSPY_ALL_URL)
        rows = []
        for appid_str, entry in data.items():
            try:
                appid = int(entry.get("appid") or appid_str)
            except Exception:
                continue
            rows.append(
                (
                    appid,
                    self._parse_owners_mid(entry.get("owners", "0..0")),
                    int(entry.get("ccu", 0) or 0),
                    int(entry.get("userscore", 0) or 0),
                    int(entry.get("average_2weeks", 0) or 0),
                )
            )
        return rows

    def _fts_match(self, query):
        normalized = self._normalize(query)
        if not normalized:
            return ""
        tokens = [token for token in normalized.split() if token]
        return " ".join([f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens])

    def _search_db(self, db_path, query, limit, use_popularity):
        conn = sqlite3.connect(db_path)
        try:
            match = self._fts_match(query)
            if not match:
                return []
            if use_popularity:
                conn.create_function("log", 1, lambda x: 0.0 if x is None or x <= 0 else math.log(x))
                pop_expr = (
                    " (CASE WHEN p.owners_mid>0 THEN log(p.owners_mid+1) ELSE 0 END)"
                    " + 0.6*(CASE WHEN p.ccu>0 THEN log(p.ccu+1) ELSE 0 END)"
                    " + 0.2*(COALESCE(p.userscore,0)/100.0)"
                )
                sql = f"""
                    SELECT a.appid, a.name, {pop_expr} AS popscore
                    FROM apps_fts f
                    JOIN apps a ON a.appid=f.rowid
                    LEFT JOIN pop p ON p.appid=a.appid
                    WHERE apps_fts MATCH ?
                    ORDER BY popscore DESC, length(a.name) ASC, a.name ASC
                    LIMIT ?
                """
                rows = conn.execute(sql, (match, limit)).fetchall()
            else:
                rows = conn.execute(
                    """
                        SELECT a.appid, a.name
                        FROM apps_fts f
                        JOIN apps a ON a.appid=f.rowid
                        WHERE apps_fts MATCH ?
                        ORDER BY length(a.name) ASC, a.name ASC
                        LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
            if not rows:
                like = f"%{self._normalize(query)}%"
                rows = conn.execute(
                    "SELECT appid, name FROM apps WHERE name_norm LIKE ? ORDER BY length(name), name LIMIT ?",
                    (like, limit),
                ).fetchall()
            results = []
            for row in rows:
                appid, name = row[0], row[1]
                results.append((str(appid), name.strip()))
            return results
        finally:
            conn.close()

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
    icon_path = resolve_asset_path("steam_icon.ico")
    if not icon_path:
        logger.warning("Application icon steam_icon.ico not found; using default Tk icon.")
        return
    try:
        root.iconbitmap(str(icon_path))
    except Exception as exc:
        logger.warning("Failed to apply application icon at %s: %s", icon_path, exc)


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
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        ensure_icon(self.root)
        self.create_background()
        self.queue = queue.Queue()
        self.search_var = tk.StringVar()
        self.search_after_id = None
        self.status_var = tk.StringVar(value=STATUS_IDLE)
        self.download_log_var = tk.StringVar(value="")
        self.search_manager = SteamSearchManager(
            limit=STEAM_SEARCH_LIMIT,
            status_callback=self.handle_search_status,
        )
        self.handle_search_status("Getting search ready...")
        self.repos = read_repos(Path("repos.txt"))
        self.repo_objects = {identifier: ManifestRepo(identifier) for identifier in self.repos}
        self.store = LocalStore("added_games.json")
        steam_root = detect_steam_path()
        self.steam_folder_var = tk.StringVar(value=str(steam_root))
        self.integrator = GreenLumaIntegrator(steam_root)
        self.latest_results = {}
        self.app_name_cache = {}
        self.search_entry = None
        self.search_placeholder_active = False
        self.active_search = None
        self.drop_zone_default_text = "Drop .zip file here"
        self.drop_zone_hint_text = "Drag a .zip archive onto this window to import locally"
        self._drop_zone_reset_job = None
        self.import_in_progress = False
        self.drop_overlay = None
        self.drop_overlay_inner = None
        self.drop_overlay_icon = None
        self.drop_overlay_label = None
        self.drop_overlay_hint = None
        self.drop_overlay_visible = False
        self.setup_style()
        self.build_gui()
        self.set_drop_zone_state("idle")
        self.refresh_added()
        self.setup_drag_and_drop()
        self.search_manager.start()
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
        search_entry.bind("<KeyPress>", self.on_search_keypress)
        self.search_entry = search_entry
        self.search_placeholder = "Search by name or app ID"
        self.search_placeholder_color = "#4f6b8c"
        self.search_normal_color = "#e7f1ff"
        self.apply_search_placeholder(search_entry, init=True)
        search_entry.bind("<FocusIn>", self.on_search_focus_in)
        search_entry.bind("<FocusOut>", self.on_search_focus_out)
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
        self.create_drop_overlay()
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

    def create_drop_overlay(self):
        self.drop_overlay = tk.Frame(self.root, bg="#102136", highlightthickness=2, highlightbackground="#225c8d")
        self.drop_overlay.place_forget()
        self.drop_overlay_inner = tk.Frame(self.drop_overlay, bg="#102136")
        self.drop_overlay_inner.place(relx=0.5, rely=0.5, anchor="center")
        self.drop_overlay_icon = tk.Canvas(self.drop_overlay_inner, width=48, height=48, bg="#102136", highlightthickness=0)
        self.drop_overlay_icon.grid(row=0, column=0, pady=(0, 8))
        self._draw_drop_icon(self.drop_overlay_icon, "#4a7fb5")
        self.drop_overlay_label = tk.Label(
            self.drop_overlay_inner,
            text=self.drop_zone_default_text,
            font=("Segoe UI Semibold", 12),
            fg="#c5e2ff",
            bg="#102136",
        )
        self.drop_overlay_label.grid(row=1, column=0)
        self.drop_overlay_hint = tk.Label(
            self.drop_overlay_inner,
            text=self.drop_zone_hint_text,
            font=("Segoe UI", 10),
            fg="#7ca6c7",
            bg="#102136",
        )
        self.drop_overlay_hint.grid(row=2, column=0, pady=(4, 0))
        self.drop_overlay.lift()
        self.set_drop_zone_state("idle")

    def apply_tree_tags(self, tree):
        tree.tag_configure("even", background="#142233", foreground="#d6e6ff")
        tree.tag_configure("odd", background="#122030", foreground="#c9dbf8")
        tree.tag_configure("header", font=("Segoe UI Semibold", 10))

    def _draw_drop_icon(self, canvas, color):
        canvas.delete("icon")
        canvas.create_polygon(
            12,
            16,
            24,
            8,
            36,
            16,
            36,
            36,
            12,
            36,
            outline=color,
            fill="",
            width=2,
            tags="icon",
        )
        canvas.create_line(12, 22, 36, 22, fill=color, width=2, tags="icon")
        canvas.create_rectangle(18, 26, 30, 36, outline=color, width=2, tags="icon")

    def set_drop_zone_state(self, state, message=None, transient=False):
        if not self.drop_overlay:
            return
        if self._drop_zone_reset_job:
            self.root.after_cancel(self._drop_zone_reset_job)
            self._drop_zone_reset_job = None
        if state == "idle":
            if not self.import_in_progress and self.drop_overlay_visible:
                self.drop_overlay.place_forget()
                self.drop_overlay_visible = False
            return
        palettes = {
            "active": ("#15314a", "#3a8ee6", "#6fb0f5"),
            "success": ("#123824", "#2f9d65", "#69d49d"),
            "error": ("#341b1f", "#c15a5a", "#e38e8e"),
        }
        bg, border, icon_color = palettes.get(state, ("#102136", "#225c8d", "#4a7fb5"))
        if not self.drop_overlay_visible:
            self.drop_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.drop_overlay_visible = True
        self.drop_overlay.lift()
        self.drop_overlay.configure(bg=bg, highlightbackground=border)
        self.drop_overlay_inner.configure(bg=bg)
        self.drop_overlay_icon.configure(bg=bg)
        self.drop_overlay_label.configure(bg=bg, text=message or self.drop_zone_default_text)
        self.drop_overlay_hint.configure(bg=bg, text=self.drop_zone_hint_text)
        self._draw_drop_icon(self.drop_overlay_icon, icon_color)
        if transient:
            self._drop_zone_reset_job = self.root.after(2000, lambda: self.set_drop_zone_state("idle"))

    def setup_drag_and_drop(self):
        self.drop_target = None
        if os.name == "nt":
            try:
                self.drop_target = WindowsZipDropTarget(
                    self.root,
                    self.queue,
                )
            except Exception:
                logger.exception("Failed to initialize drag-and-drop")

    def infer_app_name_from_zip(self, lua_files, zip_path):
        for lua_file in lua_files:
            try:
                content = lua_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            match = re.search(r'appname\s*=\s*"([^\"]+)"', content, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                if candidate:
                    return candidate
            match = re.search(r'addappid\(\s*\d+\s*,\s*\d+\s*,\s*"([^\"]+)"', content, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                if candidate and not candidate.lower().startswith("0x"):
                    return candidate
        stem = zip_path.stem.strip().replace("_", " ").replace("-", " ")
        return stem or None

    def start_zip_import(self, zip_path):
        zip_path = Path(zip_path)
        if not zip_path.exists():
            message = f"{zip_path.name} not found."
            self.set_download_log(message)
            self.set_drop_zone_state("error", message, transient=True)
            return
        if self.import_in_progress:
            message = "Import already in progress."
            self.set_download_log(message)
            self.set_drop_zone_state("active", message)
            return
        self.import_in_progress = True
        self.status_var.set(f"Importing {zip_path.stem} from zip...")
        self.set_download_log(f"Importing {zip_path.name}")
        self.set_drop_zone_state("active", f"Importing {zip_path.name}")
        thread = threading.Thread(target=self.zip_import_worker, args=(zip_path,), daemon=True)
        thread.start()

    def zip_import_worker(self, zip_path):
        temp_dir = Path(tempfile.mkdtemp(prefix="steam_zip_import_"))
        try:
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    archive.extractall(temp_dir)
            except Exception as exc:
                logger.exception("Failed to extract %s", zip_path)
                self.queue.put(("zip_error", zip_path.name, f"Failed to extract {zip_path.name}: {exc}"))
                return
            extracted_files = [path for path in temp_dir.rglob("*") if path.is_file()]
            lua_files = [path for path in extracted_files if path.suffix.lower() == ".lua"]
            manifest_files = [path for path in extracted_files if path.suffix.lower() == ".manifest"]
            key_files = [path for path in extracted_files if path.suffix.lower() == ".key"]
            missing = []
            if not lua_files:
                missing.append(".lua")
            if not manifest_files:
                missing.append(".manifest")
            if missing:
                message = f"{zip_path.name}: missing {' and '.join(missing)} file(s)"
                self.queue.put(("zip_error", zip_path.name, message))
                return
            app_ids = sorted({ "".join(ch for ch in path.stem if ch.isdigit()) for path in lua_files })
            app_ids = [appid for appid in app_ids if appid]
            if not app_ids:
                self.queue.put(("zip_error", zip_path.name, f"{zip_path.name}: unable to determine app ID"))
                return
            if len(app_ids) > 1:
                self.queue.put(("zip_error", zip_path.name, f"{zip_path.name}: multiple app IDs detected ({', '.join(app_ids)})"))
                return
            appid = app_ids[0]
            app_name = self.resolve_app_name(appid)
            if not app_name or app_name == f"App {appid}":
                derived = self.infer_app_name_from_zip(lua_files, zip_path)
                if derived:
                    app_name = derived
            allowed_depots = sorted({
                manifest.stem.split("_", 1)[0]
                for manifest in manifest_files
                if "_" in manifest.stem and manifest.stem.split("_", 1)[0].isdigit()
            })
            files_to_apply = lua_files + key_files + manifest_files
            manifest_files_moved, depot_keys, applist_files, rename_map = self.integrator.apply(
                appid,
                app_name,
                files_to_apply,
                allowed_depots=allowed_depots,
                progress_callback=self.set_download_log,
            )
            if rename_map:
                self.store.update_applist_files(rename_map)
            repo_id = f"local_zip/{zip_path.stem}"
            self.store.add(repo_id, appid, app_name, manifest_files_moved, depot_keys, applist_files)
            self.queue.put(("zip_success", zip_path.name, appid, app_name))
        except Exception as exc:
            logger.exception("Unexpected error while importing %s", zip_path)
            self.queue.put(("zip_error", zip_path.name, f"Failed to import {zip_path.name}: {exc}"))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def choose_steam_folder(self):
        folder = filedialog.askdirectory(title="Select Steam Folder", initialdir=self.steam_folder_var.get())
        if folder:
            self.steam_folder_var.set(folder)
            self.integrator = GreenLumaIntegrator(folder)
            messagebox.showinfo("Steam Folder", "Steam folder updated.", parent=self.root)

    def apply_search_placeholder(self, entry_widget, init=False):
        text = self.search_var.get()
        if text and not self.search_placeholder_active:
            entry_widget.configure(foreground=self.search_normal_color)
            return
        entry_widget.configure(foreground=self.search_placeholder_color)
        self.search_placeholder_active = True
        self.search_var.set(self.search_placeholder)
        entry_widget.icursor(0)
        if not init:
            entry_widget.selection_clear()

    def clear_search_placeholder(self, entry_widget):
        if not self.search_placeholder_active:
            return
        self.search_placeholder_active = False
        self.search_var.set("")
        entry_widget.configure(foreground=self.search_normal_color)

    def on_search_focus_in(self, event):
        widget = event.widget
        self.clear_search_placeholder(widget)

    def on_search_focus_out(self, event):
        widget = event.widget
        if not self.search_var.get():
            self.apply_search_placeholder(widget)

    def on_search_keypress(self, event):
        if self.search_placeholder_active:
            self.clear_search_placeholder(event.widget)

    def resolve_app_name(self, appid):
        cached = self.app_name_cache.get(appid)
        if cached:
            return cached
        url = STORE_APP_DETAILS_URL_TEMPLATE.format(appid=appid, language=DEFAULT_LANGUAGE)
        try:
            data = request_json(url)
            entry = data.get(str(appid), {})
            if entry.get("success"):
                store_data = entry.get("data") or {}
                name = (store_data.get("name") or "").strip()
                if name:
                    self.app_name_cache[appid] = name
                    return name
        except Exception as exc:
            logger.warning("Failed to resolve app name for app id %s: %s", appid, exc)
        fallback = f"App {appid}"
        self.app_name_cache[appid] = fallback
        return fallback

    def on_search_change(self, event=None):
        widget = event.widget if event else None
        if widget and self.search_placeholder_active:
            return
        if self.search_after_id:
            self.root.after_cancel(self.search_after_id)
        self.search_after_id = self.root.after(SEARCH_DELAY_MS, self.start_search)

    def start_search(self):
        raw_value = self.search_var.get()
        if self.search_placeholder_active:
            query = ""
        else:
            query = raw_value.strip()
        if not query:
            if not self.search_placeholder_active:
                self.search_var.set("")
            self.clear_results()
            self.status_var.set(STATUS_IDLE)
            if self.search_entry:
                self.apply_search_placeholder(self.search_entry)
            return
        if self.active_search:
            self.active_search = None
        self.status_var.set("Searching...")
        thread = threading.Thread(target=self.search_worker, args=(query,), daemon=True)
        self.active_search = thread
        thread.start()

    def search_worker(self, query):
        try:
            results = []
            normalized = query.strip()

            def inspect_app(appid, name):
                repos = []
                for repo_id, repo in self.repo_objects.items():
                    try:
                        logger.info("Checking repo %s for app id %s", repo_id, appid)
                        if not repo.branch_exists(appid):
                            logger.info("Repo %s has no branch for app id %s", repo_id, appid)
                            continue
                    except Exception:
                        logger.exception("Failed to inspect repo %s for app %s", repo_id, appid)
                        continue
                    repos.append({"repo": repo_id, "branch": appid})
                    logger.info("Repo %s has branch for app %s", repo_id, appid)
                if repos:
                    results.append({"appid": appid, "name": name, "repos": repos})
                    logger.info(
                        "Found app id %s (%s) with repos (branch only): %s",
                        appid,
                        name,
                        ", ".join(repo_entry["repo"] for repo_entry in repos),
                    )
                else:
                    logger.info("No manifests found for app %s (%s)", appid, name)

            if normalized.isdigit():
                appid = normalized
                name = self.resolve_app_name(appid)
                logger.info("Searching repositories for app id %s (%s)", appid, name)
                inspect_app(appid, name)
            else:
                logger.info("Searching Steam index for '%s'", normalized)
                hits = self.search_manager.search(normalized)
                if not hits:
                    logger.info("No Steam search hits for '%s'", normalized)
                for appid, name in hits:
                    inspect_app(appid, name)

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
                if kind == "drop_hover":
                    _, active = item
                    if not self.import_in_progress:
                        if active:
                            self.set_drop_zone_state("active")
                        else:
                            self.set_drop_zone_state("idle")
                elif kind == "drop_zip":
                    _, path_value = item
                    if path_value:
                        if self.import_in_progress:
                            self.set_download_log("Import already in progress.")
                            self.set_drop_zone_state("active", "Import already in progress")
                        else:
                            self.start_zip_import(Path(path_value))
                    else:
                        message = "Only .zip files are supported."
                        self.set_download_log(message)
                        self.set_drop_zone_state("error", message, transient=True)
                elif kind == "search_results":
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
                        self.set_download_log(f"Finished {name} ({appid})")
                    else:
                        _, _, message = item
                        self.status_var.set(STATUS_IDLE)
                        self.set_download_log("Download failed")
                        messagebox.showerror("Download Failed", message, parent=self.root)
                elif kind == "zip_success":
                    _, zip_name, appid, name = item
                    self.import_in_progress = False
                    self.refresh_added()
                    self.status_var.set(f"{name} imported.")
                    self.set_download_log(f"Imported {name} ({appid})")
                    self.set_drop_zone_state("success", f"Imported {name}", transient=True)
                elif kind == "zip_error":
                    _, zip_name, message = item
                    self.import_in_progress = False
                    self.status_var.set(STATUS_IDLE)
                    self.set_download_log(message)
                    self.set_drop_zone_state("error", message, transient=True)
        except queue.Empty:
            pass
        self.root.after(150, self.process_queue)

    def handle_search_status(self, message):
        logger.info("Search status: %s", message)
        def update():
            self.download_log_var.set(message)
        if threading.current_thread() is threading.main_thread():
            update()
        else:
            self.root.after(0, update)

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

    def on_close(self):
        try:
            if getattr(self, "drop_target", None):
                self.drop_target.unregister()
        except Exception:
            logger.exception("Error while releasing drop target")
        try:
            if getattr(self, "search_manager", None):
                self.search_manager.close()
        except Exception:
            logger.exception("Error while shutting down search manager")
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = SteamDepotApp()
    app.run()


if __name__ == "__main__":
    main()
