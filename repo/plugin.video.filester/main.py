import os
import re
import ssl
import sys
import json
import time
import random
import hashlib
import threading
import urllib.request
import urllib.parse
import urllib.error
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, List, Tuple
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

# ---------------------------------------------------------------------------
# Addon setup
# ---------------------------------------------------------------------------
ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE_URL = sys.argv[0] if len(sys.argv) > 0 else ""
API_BASE = "https://u1.filester.me/api/v1"
API_ROOT = "https://u1.filester.me"
PUBLIC_VIEW_URL = "https://filester.me/v2/api/public/view"
SITE_BASE = "https://filester.me"
PER_PAGE = 100
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 60
THUMB_FETCH_TIMEOUT = 8
MAX_RETRIES = 3
USER_AGENT = "Kodi-Filester/1.0"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/120.0.0.0 Safari/537.36")
CACHE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))

MEMORY_CACHE_MAX = 512
FILE_DETAILS_CACHE_MAX = 1024
THUMBNAIL_CACHE_MAX = 4096
THUMB_FETCH_WORKERS = 8
FOLDER_THUMB_CONCURRENT = 3
DOS_WARN_SECONDS = 5.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 8.0
MAX_FOLDER_NAME_LEN = 100

LISTING_REVALIDATE_SECONDS = 30
REFRESH_RATE_LIMIT_PER_MIN = 5

ALLOWED_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
MAX_THUMB_SIZE = 5 * 1024 * 1024

SESSION_THUMB_PREFIX = "Filester.FolderThumb."
SESSION_THUMB_NONE = "__NONE__"

PLACEHOLDER_THUMB_PATHS = ('/img/meta.png',)

FOLDER_THUMB_CACHE_PREFIX = "folder_thumb:"
FILE_THUMB_CACHE_PREFIX = "file_thumb:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(message: str, level: int = xbmc.LOGDEBUG) -> None:
    xbmc.log(f"Filester: {message}", level)


def notify(title: str, message: str,
           icon: int = xbmcgui.NOTIFICATION_INFO, ms: int = 3000) -> None:
    xbmcgui.Dialog().notification(title, message, icon, ms)


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ('true', '1', 'yes', 'on')


def setting_bool(setting_id: str, default: bool = True) -> bool:
    try:
        raw = ADDON.getSetting(setting_id)
    except Exception:
        return default
    if raw is None or raw == '':
        return default
    return as_bool(raw, default=default)


def quote_param(value: Any) -> str:
    return urllib.parse.quote(str(value), safe='')


def build_url(params: Dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{BASE_URL}?{urllib.parse.urlencode(clean)}"


def refresh_container() -> None:
    xbmc.executebuiltin("Container.Refresh")


def _current_plugin_folder() -> Tuple[Any, bool]:
    try:
        current = xbmc.getInfoLabel('Container.FolderPath') or ''
    except Exception:
        return ('__unknown__', False)
    if not current or not current.startswith(BASE_URL):
        return (None, False)
    try:
        parsed = urllib.parse.urlparse(current)
        qs = urllib.parse.parse_qs(parsed.query)
    except Exception:
        return ('__unknown__', True)
    action = (qs.get('action') or [''])[0]
    if action not in ('', 'list'):
        return ('__unknown__', True)
    fid = (qs.get('folder_id') or [''])[0]
    return (fid if fid else None, True)


def _is_viewing(folder_id: Optional[str]) -> bool:
    result, in_plugin = _current_plugin_folder()
    if result == '__unknown__' or not in_plugin:
        return False
    if not folder_id or folder_id in ('', '0'):
        return result is None
    return result == str(folder_id)


def _is_in_plugin() -> bool:
    try:
        current = xbmc.getInfoLabel('Container.FolderPath') or ''
    except Exception:
        return False
    return current.startswith(BASE_URL)


def _do_refresh(debounce_seconds: float) -> None:
    win = xbmcgui.Window(10000)
    now = time.time()
    try:
        last = float(win.getProperty('Filester.LastRefreshTs') or 0)
    except (ValueError, TypeError):
        last = 0.0
    if now - last < debounce_seconds:
        return
    times_key = 'Filester.RefreshTimes'
    try:
        times_str = win.getProperty(times_key) or ''
        times = [float(t) for t in times_str.split(',') if t]
    except (ValueError, TypeError):
        times = []
    times = [t for t in times if now - t < 60]
    if len(times) >= REFRESH_RATE_LIMIT_PER_MIN:
        log(f"Refresh rate-limited ({len(times)}/60s) — suppressing",
            xbmc.LOGWARNING)
        return
    times.append(now)
    win.setProperty(times_key, ','.join(f"{t:.3f}" for t in times))
    log(f"Container.Refresh (window={debounce_seconds}s count={len(times)})")
    win.setProperty('Filester.LastRefreshTs', str(now))
    xbmc.executebuiltin("Container.Refresh")


_refresh_sched_lock = threading.Lock()


def _debounced_refresh_container(target_folder_id: Optional[str],
                                  debounce_seconds: float = 2.0) -> None:
    if not _is_viewing(target_folder_id):
        return
    win = xbmcgui.Window(10000)
    ctx = target_folder_id or '__root__'
    last_key = f"Filester.LastRefreshTs.{ctx}"
    sched_key = f"Filester.RefreshScheduled.{ctx}"
    now = time.time()
    try:
        last = float(win.getProperty(last_key) or 0)
    except (ValueError, TypeError):
        last = 0.0
    elapsed = now - last
    with _refresh_sched_lock:
        has_pending = bool(win.getProperty(sched_key))
    if elapsed >= debounce_seconds and not has_pending:
        win.setProperty(last_key, str(now))
        _do_refresh(debounce_seconds)
        return
    if has_pending:
        return
    with _refresh_sched_lock:
        if win.getProperty(sched_key):
            return
        win.setProperty(sched_key, "1")
    delay = max(debounce_seconds - elapsed, 0.1) + 0.05

    def _do_trailing():
        try:
            xbmc.sleep(int(delay * 1000))
            with _refresh_sched_lock:
                win.clearProperty(sched_key)
            if _is_viewing(target_folder_id):
                win.setProperty(last_key, str(time.time()))
                _do_refresh(0.0)
        except Exception as e:
            log(f"Trailing refresh failed: {e}", xbmc.LOGWARNING)

    threading.Thread(target=_do_trailing, daemon=True).start()


def _debounced_refresh_any(debounce_seconds: float = 2.0) -> None:
    if not _is_in_plugin():
        return
    _do_refresh(debounce_seconds)


def _extract_folder_from_url(url: str) -> Any:
    if '/folders' in url and '/folder/' not in url:
        return '__any__'
    m = re.search(r'/folder/([^/]+)/files', url)
    if m:
        return urllib.parse.unquote(m.group(1))
    if '/files' in url:
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
        except Exception:
            return '__any__'
        fid = (qs.get('folder') or [''])[0]
        return fid if fid else None
    return '__any__'


def jittered_backoff(attempt: int, retry_after: Optional[float] = None) -> float:
    if retry_after is not None:
        return min(max(retry_after, 0.5), BACKOFF_MAX)
    base = min(BACKOFF_BASE ** attempt, BACKOFF_MAX)
    return base * (0.5 + random.random() * 0.5)


def _listing_signature(payload: Dict[str, Any]) -> tuple:
    if not isinstance(payload, dict):
        return (str(payload),)
    data = payload.get('data')
    if not isinstance(data, list):
        return (str(payload),)
    sig = []
    for item in data:
        if isinstance(item, dict):
            sig.append((
                item.get('id') or item.get('uuid') or '',
                item.get('name') or item.get('original_name') or '',
                item.get('created_at') or item.get('upload_date') or '',
                item.get('folder_id') or '',
                item.get('size') or item.get('size_mb') or '',
                bool(item.get('has_thumbnail')),
            ))
        else:
            sig.append(str(item))
    return tuple(sorted(sig))


def is_placeholder_thumb(url: Optional[str]) -> bool:
    if not url:
        return False
    path = url.split('?', 1)[0].rstrip('/')
    return path.endswith(PLACEHOLDER_THUMB_PATHS)


def _resolve_thumb(thumb: str) -> str:
    if thumb.startswith(('http://', 'https://')):
        return thumb
    if thumb.startswith('/'):
        return f"{SITE_BASE}{thumb}"
    return f"{SITE_BASE}/{thumb}"


def _is_false(value: Any) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == 'false')


def inline_thumbnail(item: Dict[str, Any]) -> Optional[str]:
    for key in ('thumbnail_url', 'preview_url'):
        thumb = item.get(key)
        if thumb:
            resolved = _resolve_thumb(thumb)
            if not is_placeholder_thumb(resolved):
                return resolved
    if _is_false(item.get('has_thumbnail')):
        return None
    ident = item.get('uuid') or item.get('file_uuid') or item.get('slug')
    if ident:
        candidate = f"{SITE_BASE}/t/{ident}"
        if not is_placeholder_thumb(candidate):
            return candidate
    return None


def absolute_thumb_url(path: str) -> str:
    if path.startswith(('http://', 'https://')):
        return path
    if path.startswith('/'):
        return f"{SITE_BASE}{path}"
    return f"{SITE_BASE}/{path}"


def extract_folder_thumb(folder: Dict[str, Any]) -> Optional[str]:
    for key in ('thumbnail_url', 'thumbnail', 'thumb',
                'folder_thumbnail', 'folder_thumbnail_url',
                'cover', 'cover_url', 'image', 'image_url', 'icon'):
        val = folder.get(key)
        if not val:
            continue
        if isinstance(val, str):
            resolved = absolute_thumb_url(val)
            if not is_placeholder_thumb(resolved):
                return resolved
            continue
        if isinstance(val, dict):
            for sub in ('url', 'path', 'src', 'href'):
                subval = val.get(sub)
                if subval:
                    resolved = absolute_thumb_url(str(subval))
                    if not is_placeholder_thumb(resolved):
                        return resolved
    return None


def parse_upload_date(raw: Any) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        cleaned = raw.replace('T', ' ').rstrip('Z').strip()
        return cleaned.split('.')[0]
    except Exception:
        return None


def build_plot(file: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []

    try:
        size_bytes = int(file.get('size') or 0)
        if size_bytes > 0:
            # use same formatting as the new version
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if size_bytes < 1024 or unit == "TB":
                    parts.append(f"Size: {size_bytes:.1f} {unit}"
                                 if unit != "B" else f"Size: {size_bytes} B")
                    break
                size_bytes /= 1024.0
    except (ValueError, TypeError):
        pass

    date = str(file.get('created_at') or '')[:10]
    if date:
        parts.append(f"Date: {date}")

    downloads = file.get('downloads')
    if downloads:
        parts.append(f"Downloads: {downloads}")

    return "\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Session-scoped in-memory thumbnail cache (not disk)
# ---------------------------------------------------------------------------
def session_get_thumb(folder_id: str) -> Optional[str]:
    try:
        return xbmcgui.Window(10000).getProperty(
            f"{SESSION_THUMB_PREFIX}{folder_id}"
        ) or None
    except Exception:
        return None


def session_set_thumb(folder_id: str, url: str) -> None:
    try:
        xbmcgui.Window(10000).setProperty(
            f"{SESSION_THUMB_PREFIX}{folder_id}", url
        )
    except Exception:
        pass


def session_clear_thumb(folder_id: str) -> None:
    try:
        xbmcgui.Window(10000).clearProperty(
            f"{SESSION_THUMB_PREFIX}{folder_id}"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Bounded TTL cache
# ---------------------------------------------------------------------------
class TTLCache:
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._store: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, ttl: int) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if ttl != 0 and (time.time() - ts) >= ttl:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def get_with_age(self, key: str) -> Optional[Tuple[float, Any]]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            self._store.move_to_end(key)
            return (time.time() - ts, value)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def keys_matching(self, substring: str) -> List[str]:
        with self._lock:
            return [k for k in self._store if substring in k]

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class FilesterAPI:
    _NON_RETRYABLE = {400, 401, 403, 404, 422}
    _THUMB_MISSING = ''

    def __init__(self) -> None:
        self.api_key = self._prompt_api_key()
        self.cache_enabled = setting_bool('cache_enabled', default=True)
        self.cache_expiry = self._read_cache_expiry()
        self._ensure_cache_dir()

        self._memory_cache = TTLCache(MEMORY_CACHE_MAX)
        self._file_details_cache = TTLCache(FILE_DETAILS_CACHE_MAX)
        self._thumbnail_cache = TTLCache(THUMBNAIL_CACHE_MAX)

        self._inflight: Dict[str, threading.Lock] = {}
        self._inflight_lock = threading.Lock()

        self._revalidating: set = set()
        self._reval_lock = threading.Lock()

        self._folder_thumb_sem = threading.Semaphore(FOLDER_THUMB_CONCURRENT)

    @staticmethod
    def _prompt_api_key() -> Optional[str]:
        key = ADDON.getSetting('api_key') or ''
        if not key and HANDLE != -1:
            key = xbmcgui.Dialog().input(
                "Enter Filester API Key", type=xbmcgui.INPUT_ALPHANUM
            ) or ''
            if key:
                ADDON.setSetting('api_key', key)
        return key or None

    @staticmethod
    def _read_cache_expiry() -> int:
        try:
            if setting_bool('cache_expiry_unlimited', default=False):
                return 0
            minutes = int(ADDON.getSetting('cache_expiry') or 60)
            return max(minutes * 60, 60)
        except Exception:
            return 3600

    @staticmethod
    def _ensure_cache_dir() -> None:
        if not xbmcvfs.exists(CACHE_DIR):
            xbmcvfs.mkdirs(CACHE_DIR)

    def is_cache_valid(self, timestamp: float) -> bool:
        if not self.cache_enabled:
            return False
        if self.cache_expiry == 0:
            return True
        return (time.time() - timestamp) < self.cache_expiry

    @staticmethod
    def _cache_file_path(url: str) -> str:
        digest = hashlib.sha256(url.encode('utf-8')).hexdigest()
        return os.path.join(CACHE_DIR, f"{digest}.json")

    def _read_disk_cache(self, url: str) -> Optional[Dict[str, Any]]:
        if not self.cache_enabled:
            return None
        path = self._cache_file_path(url)
        if not xbmcvfs.exists(path):
            return None
        try:
            with xbmcvfs.File(path, 'r') as f:
                payload = json.loads(f.read())
            if self.is_cache_valid(payload.get('timestamp', 0)):
                return payload.get('data')
        except Exception as e:
            log(f"Failed to read cache {path}: {e}")
        return None

    def _read_disk_cache_raw(self, url: str) -> Optional[Tuple[float, Any]]:
        if not self.cache_enabled:
            return None
        path = self._cache_file_path(url)
        if not xbmcvfs.exists(path):
            return None
        try:
            with xbmcvfs.File(path, 'r') as f:
                payload = json.loads(f.read())
            return (payload.get('timestamp', 0), payload.get('data'))
        except Exception as e:
            log(f"Failed to read cache {path}: {e}")
        return None

    def _write_disk_cache(self, url: str, data: Dict[str, Any]) -> None:
        if not self.cache_enabled:
            return
        try:
            payload = json.dumps({'timestamp': time.time(), 'data': data})
            with xbmcvfs.File(self._cache_file_path(url), 'w') as f:
                f.write(payload)
        except Exception as e:
            log(f"Failed to write cache: {e}")

    def _lock_for(self, url: str) -> threading.Lock:
        with self._inflight_lock:
            lock = self._inflight.get(url)
            if lock is None:
                lock = threading.Lock()
                self._inflight[url] = lock
            return lock

    def _read_caches(self, url: str) -> Optional[Dict[str, Any]]:
        cached = self._memory_cache.get(url, self.cache_expiry)
        if cached is not None:
            return cached
        cached = self._read_disk_cache(url)
        if cached is not None:
            self._memory_cache.set(url, cached)
            return cached
        return None

    def _peek_cache(self, url: str) -> Optional[Tuple[float, Any]]:
        entry = self._memory_cache.get_with_age(url)
        if entry is not None:
            return entry
        raw = self._read_disk_cache_raw(url)
        if raw is not None:
            ts, data = raw
            self._memory_cache.set(url, data)
            return (time.time() - ts, data)
        return None

    def _store_caches(self, url: str, payload: Dict[str, Any]) -> None:
        self._memory_cache.set(url, payload)
        self._write_disk_cache(url, payload)

    def invalidate_cache(self, url: str) -> None:
        self._memory_cache.delete(url)
        if self.cache_enabled:
            path = self._cache_file_path(url)
            try:
                if xbmcvfs.exists(path):
                    xbmcvfs.delete(path)
            except Exception as e:
                log(f"Failed to delete cache file {path}: {e}")

    def invalidate_folders_cache(self) -> None:
        self.invalidate_cache(f"{API_BASE}/folders")

    def invalidate_folder_files(self, folder_id: str) -> None:
        marker = f"/folder/{quote_param(folder_id)}/files"
        for key in self._memory_cache.keys_matching(marker):
            self._memory_cache.delete(key)
        if not self.cache_enabled:
            return
        for page in range(1, 51):
            url = (f"{API_BASE}/folder/{quote_param(folder_id)}/files"
                   f"?page={page}&per_page={PER_PAGE}")
            path = self._cache_file_path(url)
            try:
                if xbmcvfs.exists(path):
                    xbmcvfs.delete(path)
            except Exception as e:
                log(f"Failed to delete folder cache file: {e}")

    def cached_folder_thumb(self, folder_id: str) -> Tuple[bool, Optional[str]]:
        key = f"{FOLDER_THUMB_CACHE_PREFIX}{folder_id}"
        cached = self._memory_cache.get(key, self.cache_expiry)
        if cached is not None:
            return (True, cached.get('url'))
        raw = self._read_disk_cache_raw(key)
        if raw is not None:
            ts, data = raw
            if self.is_cache_valid(ts):
                self._memory_cache.set(key, data)
                return (True, data.get('url'))
        return (False, None)

    def cache_folder_thumb(self, folder_id: str, url: Optional[str]) -> None:
        key = f"{FOLDER_THUMB_CACHE_PREFIX}{folder_id}"
        payload = {'url': url}
        self._memory_cache.set(key, payload)
        if url is not None:
            self._write_disk_cache(key, payload)

    def invalidate_folder_thumb(self, folder_id: str) -> None:
        self.invalidate_cache(f"{FOLDER_THUMB_CACHE_PREFIX}{folder_id}")

    def _auth_headers(self) -> Dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def request(self, endpoint: str, method: str = 'GET',
                data: Optional[Dict[str, Any]] = None,
                retries: int = MAX_RETRIES) -> Dict[str, Any]:
        url = endpoint if endpoint.startswith('http') else f"{API_BASE}{endpoint}"
        if method != 'GET' or not self.cache_enabled:
            return self._http_call(url, method, data, retries)
        cached = self._read_caches(url)
        if cached is not None:
            return cached
        with self._lock_for(url):
            cached = self._read_caches(url)
            if cached is not None:
                return cached
            payload = self._http_call(url, method, data, retries)
            self._store_caches(url, payload)
            return payload

    def request_swr(self, endpoint: str, method: str = 'GET',
                    data: Optional[Dict[str, Any]] = None,
                    retries: int = MAX_RETRIES,
                    revalidate_seconds: int = LISTING_REVALIDATE_SECONDS
                    ) -> Dict[str, Any]:
        url = endpoint if endpoint.startswith('http') else f"{API_BASE}{endpoint}"
        if method != 'GET' or not self.cache_enabled:
            return self._http_call(url, method, data, retries)
        cached = self._peek_cache(url)
        if cached is None:
            with self._lock_for(url):
                cached = self._peek_cache(url)
                if cached is None:
                    payload = self._http_call(url, method, data, retries)
                    self._store_caches(url, payload)
                    return payload
            return cached[1]
        age, cached_data = cached
        if age >= revalidate_seconds:
            self._schedule_revalidation(url, method, data, retries, cached_data)
        return cached_data

    def _schedule_revalidation(self, url: str, method: str,
                                data: Optional[Dict[str, Any]], retries: int,
                                old_data: Dict[str, Any]) -> None:
        with self._reval_lock:
            if url in self._revalidating:
                return
            self._revalidating.add(url)
        target_folder = _extract_folder_from_url(url)

        def worker() -> None:
            try:
                fresh = self._http_call(url, method, data, retries,
                                        cache_bust=True)
                if _listing_signature(fresh) != _listing_signature(old_data):
                    self._store_caches(url, fresh)
                    log(f"SWR: content changed for {url} — refreshing")
                    if target_folder == '__any__':
                        _debounced_refresh_any(debounce_seconds=3.0)
                    else:
                        _debounced_refresh_container(target_folder,
                                                      debounce_seconds=3.0)
            except Exception as e:
                log(f"SWR revalidation failed for {url}: {e}", xbmc.LOGWARNING)
            finally:
                with self._reval_lock:
                    self._revalidating.discard(url)

        threading.Thread(target=worker, daemon=True).start()

    def _http_call(self, url: str, method: str,
                    data: Optional[Dict[str, Any]], retries: int,
                    cache_bust: bool = False) -> Dict[str, Any]:
        if retries <= 0:
            raise RuntimeError(f"request({url}) called with retries <= 0")
        last_error: Optional[Exception] = None
        ssl_bypassed = False
        for attempt in range(retries):
            retry_after: Optional[float] = None
            try:
                headers = self._auth_headers()
                body: Optional[bytes] = None
                if method in ('POST', 'PUT', 'PATCH') and data is not None:
                    body = json.dumps(data).encode('utf-8')
                    headers["Content-Type"] = "application/json"
                if cache_bust:
                    sep = '&' if '?' in url else '?'
                    request_url = f"{url}{sep}_t={int(time.time() * 1000)}"
                    headers["Cache-Control"] = "no-cache"
                    headers["Pragma"] = "no-cache"
                else:
                    request_url = url
                req = urllib.request.Request(
                    request_url, data=body, headers=headers, method=method
                )
                if ssl_bypassed:
                    ctx = ssl._create_unverified_context()
                    with urllib.request.urlopen(
                        req, timeout=REQUEST_TIMEOUT, context=ctx
                    ) as resp:
                        raw = resp.read().decode('utf-8')
                else:
                    with urllib.request.urlopen(
                        req, timeout=REQUEST_TIMEOUT
                    ) as resp:
                        raw = resp.read().decode('utf-8')
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as e:
                    last_error = e
                    log(f"Invalid JSON from {url} "
                        f"(attempt {attempt + 1}/{retries})", xbmc.LOGWARNING)
                else:
                    if not payload.get('success', True):
                        raise Exception(
                            f"API Error: {payload.get('message', 'Unknown')}"
                        )
                    return payload
            except urllib.error.HTTPError as e:
                if e.code in self._NON_RETRYABLE:
                    log(f"HTTP {e.code} on {url} — not retrying",
                        xbmc.LOGERROR)
                    raise
                ra = e.headers.get('Retry-After') if e.headers else None
                if ra:
                    try:
                        retry_after = float(ra)
                    except ValueError:
                        retry_after = None
                last_error = e
                log(f"HTTP error {e.code} (attempt {attempt + 1}/{retries})",
                    xbmc.LOGWARNING)
            except urllib.error.URLError as e:
                reason = getattr(e, 'reason', None)
                is_ssl_error = (
                    isinstance(reason, ssl.SSLCertVerificationError)
                    or 'CERTIFICATE_VERIFY_FAILED' in str(reason)
                    or 'certificate verify failed' in str(reason).lower()
                )
                if is_ssl_error and not ssl_bypassed:
                    log(f"SSL cert failed for {url}: {reason}. Bypassing.",
                        xbmc.LOGERROR)
                    notify("Filester",
                           "SSL certificate expired — bypassing verification",
                           xbmcgui.NOTIFICATION_WARNING, ms=5000)
                    ssl_bypassed = True
                    continue
                last_error = e
                log(f"Network error (attempt {attempt + 1}/{retries}): {e}",
                    xbmc.LOGWARNING)
            except (ConnectionResetError, TimeoutError) as e:
                last_error = e
                log(f"Network error (attempt {attempt + 1}/{retries}): {e}",
                    xbmc.LOGWARNING)
            except Exception:
                raise
            if attempt < retries - 1:
                time.sleep(jittered_backoff(attempt, retry_after))
        if last_error is None:
            last_error = RuntimeError(
                f"request({url}) failed with no captured error"
            )
        raise last_error

    def get_folders(self) -> List[Dict[str, Any]]:
        return self.request_swr("/folders").get('data', []) or []

    def get_files(self, page: int = 1,
                  folder_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {'page': page, 'per_page': PER_PAGE}
        if folder_id:
            params['folder'] = folder_id
        return self.request_swr(f"/files?{urllib.parse.urlencode(params)}")

    def get_folder_files(self, folder_identifier: str,
                         page: int = 1) -> Dict[str, Any]:
        folder = quote_param(folder_identifier)
        return self.request_swr(
            f"/folder/{folder}/files?page={page}&per_page={PER_PAGE}"
        )

    def get_file_details(self, file_id: str) -> Dict[str, Any]:
        if self.cache_enabled:
            cached = self._file_details_cache.get(file_id, self.cache_expiry)
            if cached is not None:
                return cached
        try:
            details = self.request(
                f"/file/{quote_param(file_id)}"
            ).get('data', {}) or {}
        except Exception as e:
            log(f"Failed to fetch details for {file_id}: {e}",
                xbmc.LOGWARNING)
            return {}
        if details and self.cache_enabled:
            self._file_details_cache.set(file_id, details)
        return details

    def cached_thumbnail(self, file_id: str) -> Tuple[bool, Optional[str]]:
        if not self.cache_enabled:
            return (False, None)
        val = self._thumbnail_cache.get(file_id, self.cache_expiry)
        if val is not None:
            if val == self._THUMB_MISSING:
                return (True, None)
            return (True, val)
        key = f"{FILE_THUMB_CACHE_PREFIX}{file_id}"
        raw = self._read_disk_cache_raw(key)
        if raw is not None:
            ts, data = raw
            if self.is_cache_valid(ts):
                url = data.get('url') if data else None
                self._thumbnail_cache.set(
                    file_id, url if url else self._THUMB_MISSING
                )
                return (True, url)
        return (False, None)

    def _store_file_thumb(self, file_id: str, url: Optional[str]) -> None:
        if not self.cache_enabled:
            return
        self._thumbnail_cache.set(
            file_id, url if url else self._THUMB_MISSING
        )
        self._write_disk_cache(
            f"{FILE_THUMB_CACHE_PREFIX}{file_id}", {'url': url}
        )

    def thumbnail_for_file(self, file_id: str) -> Optional[str]:
        known, url = self.cached_thumbnail(file_id)
        if known:
            return url
        details = self.get_file_details(file_id)
        inline = inline_thumbnail(details)
        self._store_file_thumb(file_id, inline)
        return inline or None

    def fetch_public_folder_thumb(
        self, folder_id: str
    ) -> Tuple[bool, Optional[str]]:
        url = f"{SITE_BASE}/f/{quote_param(folder_id)}"
        headers = {
            "User-Agent": WEB_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(
                    req, timeout=THUMB_FETCH_TIMEOUT
                ) as resp:
                    html = resp.read().decode('utf-8', errors='replace')
            except urllib.error.URLError as e:
                reason = getattr(e, 'reason', None)
                is_ssl_error = (
                    isinstance(reason, ssl.SSLCertVerificationError)
                    or 'CERTIFICATE_VERIFY_FAILED' in str(reason)
                    or 'certificate verify failed' in str(reason).lower()
                )
                if not is_ssl_error:
                    raise
                log(f"SSL error on public thumb {url}, retry unverified: "
                    f"{reason}", xbmc.LOGWARNING)
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(
                    req, timeout=THUMB_FETCH_TIMEOUT, context=ctx
                ) as resp:
                    html = resp.read().decode('utf-8', errors='replace')
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+'
                r'content=["\']([^"\']+)["\']', html, re.IGNORECASE
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
                    r'property=["\']og:image["\']', html, re.IGNORECASE
                )
            if m:
                thumb = m.group(1)
                resolved = absolute_thumb_url(thumb)
                if is_placeholder_thumb(resolved):
                    log(f"Ignoring placeholder og:image for {folder_id}")
                    return (True, None)
                log(f"Public folder thumb for {folder_id}: {resolved}")
                return (True, resolved)
            log(f"No og:image on public page for {folder_id}", xbmc.LOGDEBUG)
            return (True, None)
        except urllib.error.HTTPError as e:
            log(f"Public folder page HTTP {e.code} for {folder_id}",
                xbmc.LOGWARNING)
            return (e.code in (404, 410), None)
        except Exception as e:
            log(f"Public folder thumb fetch failed for {folder_id}: {e}",
                xbmc.LOGWARNING)
            return (False, None)

    def upload_folder_thumbnail(self, folder_id: str,
                                 image_path: str) -> Optional[str]:
        try:
            with open(image_path, 'rb') as f:
                image_data = f.read()
        except Exception:
            try:
                with xbmcvfs.File(image_path, 'rb') as f:
                    image_data = f.read()
            except Exception as e:
                log(f"Failed to read image {image_path}: {e}", xbmc.LOGERROR)
                return None
        if not image_data:
            log(f"Empty image data for {image_path}", xbmc.LOGERROR)
            return None
        if len(image_data) > MAX_THUMB_SIZE:
            log(f"Image too large: {len(image_data)} bytes", xbmc.LOGERROR)
            return None
        ext = image_path.lower().rsplit('.', 1)[-1] if '.' in image_path else 'jpg'
        content_types = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
        }
        content_type = content_types.get(ext, 'application/octet-stream')
        filename = f"thumbnail.{ext}"
        boundary = '----KodiFilesterBoundary' + hashlib.md5(
            str(time.time()).encode('utf-8')
        ).hexdigest()
        boundary_bytes = boundary.encode('utf-8')
        parts: List[bytes] = []
        parts.append(b'--' + boundary_bytes + b'\r\n')
        parts.append(b'Content-Disposition: form-data; name="folder"\r\n\r\n')
        parts.append(folder_id.encode('utf-8') + b'\r\n')
        parts.append(b'--' + boundary_bytes + b'\r\n')
        parts.append(
            f'Content-Disposition: form-data; name="thumbnail"; '
            f'filename="{filename}"\r\n'.encode('utf-8')
        )
        parts.append(f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'))
        parts.append(image_data + b'\r\n')
        parts.append(b'--' + boundary_bytes + b'--\r\n')
        body = b''.join(parts)
        url = f"{API_BASE}/folder/thumbnail"
        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(body)),
            'User-Agent': USER_AGENT,
        }
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        try:
            req = urllib.request.Request(
                url, data=body, headers=headers, method='POST'
            )
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as resp:
                raw = resp.read().decode('utf-8')
            parsed = json.loads(raw)
            if not parsed.get('success'):
                log(f"Upload API error: {parsed.get('message', 'Unknown')}",
                    xbmc.LOGERROR)
                return None
            thumb_path = (parsed.get('data') or {}).get('thumbnail_url')
            if not thumb_path:
                log("Upload succeeded but no thumbnail_url", xbmc.LOGERROR)
                return None
            resolved = absolute_thumb_url(thumb_path)
            log(f"Folder thumb upload OK: {resolved}")
            return resolved
        except urllib.error.HTTPError as e:
            body_text = ''
            try:
                body_text = e.read().decode('utf-8')[:200]
            except Exception:
                pass
            log(f"Upload HTTP {e.code}: {body_text}", xbmc.LOGERROR)
            return None
        except Exception as e:
            log(f"Upload failed: {e}", xbmc.LOGERROR)
            return None

    def get_stream_url(self, file_id: str) -> Optional[str]:
        try:
            details = self.get_file_details(file_id)
            slug = details.get('slug')
            if not slug:
                raise ValueError("File slug missing from API metadata")
            view = self.request(
                PUBLIC_VIEW_URL, method='POST', data={"file_slug": slug}
            )
            server = view.get('server') or 'https://cn1.filester.me'
            file_path = view.get('file')
            token_raw = view.get('token') or ''
            if not file_path or not token_raw:
                raise ValueError("Missing file path or token")
            token = urllib.parse.quote(token_raw, safe='')
            url = f"{server}/v2/{file_path}?token={token}"
            return f"{url}|User-Agent={USER_AGENT}&Connection=keep-alive"
        except Exception as e:
            log(f"Failed to build stream URL for {file_id}: {e}",
                xbmc.LOGERROR)
            return None

    def create_folder(self, name: str,
                      parent_id: Optional[str] = None) -> Optional[str]:
        payload: Dict[str, Any] = {"name": name}
        if parent_id:
            payload["parent"] = parent_id
        try:
            response = self.request("/folder", method='POST', data=payload)
            self.invalidate_folders_cache()
            return (response.get('data') or {}).get('identifier')
        except Exception as e:
            log(f"Create folder '{name}' failed: {e}", xbmc.LOGERROR)
            return None

    def delete_file(self, file_id: str) -> bool:
        try:
            self.request(f"{API_ROOT}/file/delete", method='POST',
                          data={"identifiers": [file_id]})
            return True
        except Exception as e:
            log(f"Delete file {file_id} failed: {e}", xbmc.LOGERROR)
            return False

    def delete_folder(self, folder_id: str) -> bool:
        try:
            self.request(f"{API_ROOT}/folder/delete", method='POST',
                          data={"identifiers": [folder_id]})
            self.invalidate_folders_cache()
            self.invalidate_folder_thumb(folder_id)
            return True
        except Exception as e:
            log(f"Delete folder {folder_id} failed: {e}", xbmc.LOGERROR)
            return False

    def move_file(self, file_id: str, folder_id: Optional[str]) -> bool:
        payload: Dict[str, Any] = {"file": file_id}
        if folder_id:
            payload["folder"] = folder_id
        try:
            self.request(f"{API_BASE}/file/move", method='POST', data=payload)
            return True
        except Exception as e:
            log(f"Move file {file_id} failed: {e}", xbmc.LOGERROR)
            return False

    def move_folder(self, folder_id: str,
                    parent_id: Optional[str]) -> bool:
        payload: Dict[str, Any] = {
            "folder": folder_id,
            "parent": parent_id if parent_id else None,
        }
        try:
            self.request(f"{API_BASE}/folder/move",
                          method='POST', data=payload)
            self.invalidate_folders_cache()
            return True
        except Exception as e:
            log(f"Move folder {folder_id} failed: {e}", xbmc.LOGERROR)
            return False

    def create_folder_and_move(self, name: str, file_id: str,
                                parent_id: Optional[str] = None
                                ) -> Optional[str]:
        new_id = self.create_folder(name, parent_id=parent_id)
        if not new_id:
            return None
        if not self.move_file(file_id, new_id):
            return None
        return new_id

    def clear_all_caches(self) -> Tuple[bool, int]:
        self._memory_cache.clear()
        self._file_details_cache.clear()
        self._thumbnail_cache.clear()
        with self._inflight_lock:
            self._inflight.clear()
        with self._reval_lock:
            self._revalidating.clear()
        deleted = 0
        try:
            if xbmcvfs.exists(CACHE_DIR):
                dirs, files = xbmcvfs.listdir(CACHE_DIR)
                for name in files:
                    if name.endswith('.json'):
                        try:
                            xbmcvfs.delete(os.path.join(CACHE_DIR, name))
                            deleted += 1
                        except Exception as e:
                            log(f"Failed to delete cache {name}: {e}")
                for d in dirs:
                    try:
                        sub_dirs, sub_files = xbmcvfs.listdir(
                            os.path.join(CACHE_DIR, d)
                        )
                        if not sub_dirs and not sub_files:
                            xbmcvfs.rmdir(os.path.join(CACHE_DIR, d))
                    except Exception:
                        pass
            return True, deleted
        except Exception as e:
            log(f"Cache clear failed: {e}", xbmc.LOGERROR)
            return False, deleted


# ---------------------------------------------------------------------------
# Directory / playback handler
# ---------------------------------------------------------------------------
class DirectoryHandler:
    FOLDER_ICON = 'DefaultFolder.png'
    VIDEO_ICON = 'DefaultVideo.png'
    NEW_FOLDER_ICON = 'DefaultFolder.png'
    PROP_FOLDER_NAME = 'Filester.FolderName'

    def __init__(self, api: FilesterAPI) -> None:
        self.api = api
        self.handle = HANDLE
        self._all_folders: Optional[List[Dict[str, Any]]] = None
        self._folder_fetch_lock = threading.Lock()
        self._folder_fetch_inflight: set = set()

    @staticmethod
    def _sort_folders(folders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        try:
            return sorted(folders,
                          key=lambda f: (f.get('name') or '').lower())
        except Exception as e:
            log(f"Folder sort failed: {e}", xbmc.LOGWARNING)
            return folders

    @staticmethod
    def _sort_files(files: List[Dict[str, Any]],
                    by_date_desc: bool) -> List[Dict[str, Any]]:
        if not files:
            return files
        DATE_FIELDS = ('created_at', 'upload_date', 'uploaded_at', 'created')

        def date_str(f):
            for field in DATE_FIELDS:
                val = f.get(field)
                if val:
                    s = str(val).replace('T', ' ').rstrip('Z').strip()
                    return s.split('.')[0]
            return ''

        def numeric_id(f):
            try:
                return int(f.get('id') or 0)
            except (ValueError, TypeError):
                return 0

        try:
            if by_date_desc:
                return sorted(files,
                              key=lambda f: (date_str(f), numeric_id(f)),
                              reverse=True)
            return sorted(
                files,
                key=lambda f: (f.get('name')
                               or f.get('original_name') or '').lower()
            )
        except Exception as e:
            log(f"File sort failed: {e}", xbmc.LOGWARNING)
            return files

    @staticmethod
    def _parent_id(folder):
        parent = folder.get('parent')
        if parent in (None, '', 0, '0'):
            return None
        return str(parent)

    @staticmethod
    def _folder_id(folder):
        fid = folder.get('id')
        return str(fid) if fid is not None else None

    def _get_all_folders(self, force_refresh: bool = False
                          ) -> List[Dict[str, Any]]:
        if not force_refresh and self._all_folders is not None:
            return self._all_folders
        if force_refresh:
            self.api.invalidate_folders_cache()
        try:
            self._all_folders = self.api.get_folders()
        except Exception as e:
            log(f"get_folders failed: {e}", xbmc.LOGERROR)
            if self._all_folders is not None:
                return self._all_folders
            raise
        return self._all_folders

    def _invalidate_folder_list(self) -> None:
        self._all_folders = None

    def _folder_name_by_id(self, folder_id):
        if not folder_id:
            return None
        try:
            fid = str(folder_id)
            for f in self._get_all_folders():
                if self._folder_id(f) == fid:
                    return f.get('name') or None
        except Exception as e:
            log(f"Folder name lookup failed for {folder_id}: {e}")
        return None

    def _build_folder_tree(self, folders, parent_id=None, level=0):
        out = []
        for folder in folders:
            if self._parent_id(folder) == parent_id:
                fid = self._folder_id(folder)
                if fid is None:
                    continue
                indent = "  " * level
                name = folder.get('name', 'Unnamed')
                out.append((fid, f"{indent}└─ {name}"))
                out.extend(self._build_folder_tree(folders, fid, level + 1))
        return out

    def _descendant_ids(self, folder_id):
        out = {str(folder_id)}
        changed = True
        while changed:
            changed = False
            for f in self._get_all_folders():
                fid = self._folder_id(f)
                if fid is None or fid in out:
                    continue
                if self._parent_id(f) in out:
                    out.add(fid)
                    changed = True
        return out

    def _thumb_for(self, item):
        inline = inline_thumbnail(item)
        if inline:
            return inline
        file_id = item.get('id')
        if not file_id:
            return None
        known, url = self.api.cached_thumbnail(str(file_id))
        return url if known else None

    def _get_folder_thumbnail(self, folder_id, context_folder_id=None):
        fid = str(folder_id)
        known, url = self.api.cached_folder_thumb(fid)
        if known:
            return url
        session = session_get_thumb(fid)
        if session == SESSION_THUMB_NONE:
            return None
        if session:
            return session
        self._schedule_folder_thumb_fetch(fid, context_folder_id)
        return None

    def _schedule_folder_thumb_fetch(self, folder_id, context_folder_id):
        with self._folder_fetch_lock:
            if folder_id in self._folder_fetch_inflight:
                return
            self._folder_fetch_inflight.add(folder_id)
        threading.Thread(
            target=self._folder_thumb_worker,
            args=(folder_id, context_folder_id),
            daemon=True,
        ).start()

    def _folder_thumb_worker(self, folder_id, context_folder_id):
        added = False
        try:
            with self.api._folder_thumb_sem:
                got_api_thumb = False
                for f in self._get_all_folders():
                    if self._folder_id(f) == folder_id:
                        api_thumb = extract_folder_thumb(f)
                        if api_thumb:
                            log(f"Thumb for {folder_id}: API field "
                                f"-> {api_thumb}")
                            self.api.cache_folder_thumb(folder_id, api_thumb)
                            session_set_thumb(folder_id, api_thumb)
                            got_api_thumb = True
                            added = True
                        break
                if not got_api_thumb:
                    definitive, web_thumb = \
                        self.api.fetch_public_folder_thumb(folder_id)
                    if not web_thumb and not definitive:
                        time.sleep(1.0)
                        definitive, web_thumb = \
                            self.api.fetch_public_folder_thumb(folder_id)
                    if web_thumb:
                        self.api.cache_folder_thumb(folder_id, web_thumb)
                        session_set_thumb(folder_id, web_thumb)
                        added = True
                    elif definitive:
                        self.api.cache_folder_thumb(folder_id, None)
                        session_set_thumb(folder_id, SESSION_THUMB_NONE)
                    else:
                        log(f"Thumb for {folder_id}: fetch failed after retry")
        except Exception as e:
            log(f"Folder thumb fetch failed for {folder_id}: {e}",
                xbmc.LOGWARNING)
        finally:
            with self._folder_fetch_lock:
                self._folder_fetch_inflight.discard(folder_id)
        if added:
            _debounced_refresh_container(context_folder_id,
                                          debounce_seconds=1.5)

    def add_new_folder_item(self, parent_id):
        if not setting_bool('show_new_folder_item', default=False):
            return
        label = "[B]➕ New Subfolder[/B]" if parent_id else "[B]➕ New Folder[/B]"
        item = xbmcgui.ListItem(label=label)
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle(label)
        info_tag.setMediaType('video')
        item.setArt({'icon': self.NEW_FOLDER_ICON,
                     'thumb': self.NEW_FOLDER_ICON})
        params = {'action': 'create_folder'}
        if parent_id:
            params['parent_id'] = parent_id
        xbmcplugin.addDirectoryItem(
            handle=self.handle, url=build_url(params),
            listitem=item, isFolder=False
        )

    def add_folder_item(self, folder, context_folder_id=None):
        folder_id = self._folder_id(folder)
        if folder_id is None:
            return
        name = folder.get('name', 'Unnamed Folder')
        item = xbmcgui.ListItem(label=name)
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle(name)
        info_tag.setMediaType('video')
        thumb = self._get_folder_thumbnail(folder_id, context_folder_id)
        if thumb:
            item.setArt({'thumb': thumb, 'poster': thumb,
                         'icon': thumb, 'banner': thumb})
        else:
            item.setArt({'icon': self.FOLDER_ICON,
                         'thumb': self.FOLDER_ICON})
        item.addContextMenuItems([
            ("Set Thumbnail…",
             f"RunPlugin({build_url({'action': 'set_folder_thumbnail', 'folder_id': folder_id})})"),
            ("Refresh Folder",
             f"RunPlugin({build_url({'action': 'refresh_folder', 'folder_id': folder_id})})"),
            ("New Subfolder",
             f"RunPlugin({build_url({'action': 'create_folder', 'parent_id': folder_id})})"),
            ("Move Folder…",
             f"RunPlugin({build_url({'action': 'move_folder_prompt', 'folder_id': folder_id})})"),
            ("Delete Folder",
             f"RunPlugin({build_url({'action': 'delete_folder', 'folder_id': folder_id})})"),
        ], replaceItems=False)
        xbmcplugin.addDirectoryItem(
            handle=self.handle,
            url=build_url({'action': 'list', 'folder_id': folder_id}),
            listitem=item, isFolder=True
        )

    def add_file_item(self, file, current_folder_name=None,
                      current_folder_id=None):
        name = file.get('name') or file.get('original_name') or 'Unknown'
        file_id = file.get('id')
        file_folder_id = file.get('folder_id')
        folder_label = None
        if file_folder_id:
            if current_folder_id and str(file_folder_id) == str(current_folder_id):
                folder_label = current_folder_name
            else:
                folder_label = self._folder_name_by_id(file_folder_id)
        item = xbmcgui.ListItem(label=name)
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle(name)
        info_tag.setMediaType('video')
        upload_date = parse_upload_date(file.get('upload_date'))
        if upload_date:
            try:
                info_tag.setDateAdded(upload_date)
            except Exception:
                pass
        plot = build_plot(file)
        if plot:
            info_tag.setPlot(plot)
        item.setProperty("IsPlayable", "true")
        if folder_label:
            item.setProperty(self.PROP_FOLDER_NAME, folder_label)
        thumb = self._thumb_for(file)
        if thumb:
            item.setArt({'thumb': thumb, 'poster': thumb, 'icon': thumb})
        else:
            item.setArt({'icon': self.VIDEO_ICON})
        menu = []
        if file_folder_id and str(file_folder_id) != str(current_folder_id or ''):
            menu.append((
                "Go to Folder",
                f"Container.Update({build_url({'action': 'list', 'folder_id': file_folder_id})})",
            ))
        menu.append((
            "Move to Folder…",
            f"RunPlugin({build_url({'action': 'move_file_prompt', 'file_id': file_id})})",
        ))
        new_folder_params = {'action': 'move_to_new_folder', 'file_id': file_id}
        if current_folder_id:
            new_folder_params['parent_id'] = current_folder_id
        menu.append((
            "Move to New Folder…",
            f"RunPlugin({build_url(new_folder_params)})",
        ))
        menu.append((
            "Delete File",
            f"RunPlugin({build_url({'action': 'delete_file', 'file_id': file_id})})",
        ))
        item.addContextMenuItems(menu, replaceItems=False)
        xbmcplugin.addDirectoryItem(
            handle=self.handle,
            url=build_url({'action': 'play', 'id': file_id}),
            listitem=item, isFolder=False
        )

    def add_next_page_item(self, action, page, **extra):
        item = xbmcgui.ListItem(label="[B]Next Page >>[/B]")
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle("Next Page")
        info_tag.setMediaType('video')
        params = {'action': action, 'page': page + 1, **extra}
        xbmcplugin.addDirectoryItem(
            handle=self.handle, url=build_url(params),
            listitem=item, isFolder=True
        )

    @staticmethod
    def _has_next_page(response, page=1):
        pagination = response.get('pagination') or {}
        if pagination:
            current = pagination.get('page') or page
            total = pagination.get('pages') or pagination.get('last_page') or 1
            return current < total
        files = response.get('data') or []
        return len(files) >= PER_PAGE

    def _files_needing_fetch(self, files):
        out = []
        for f in files:
            fid = f.get('id')
            if not fid:
                continue
            if inline_thumbnail(f):
                continue
            if _is_false(f.get('has_thumbnail')):
                continue
            known, _ = self.api.cached_thumbnail(str(fid))
            if not known:
                out.append(str(fid))
        return out

    def _schedule_thumbnail_prefetch(self, file_ids, context_folder_id=None):
        if not file_ids:
            return

        def worker():
            succeeded = 0
            pool = ThreadPoolExecutor(max_workers=THUMB_FETCH_WORKERS)
            try:
                futures = [pool.submit(self.api.thumbnail_for_file, fid)
                           for fid in file_ids]
                for fut in as_completed(futures):
                    try:
                        if fut.result():
                            succeeded += 1
                    except Exception:
                        pass
            finally:
                pool.shutdown(wait=False)
            log(f"Thumbnail prefetch: {succeeded}/{len(file_ids)} ready")
            if succeeded:
                _debounced_refresh_container(context_folder_id,
                                              debounce_seconds=1.0)

        threading.Thread(target=worker, daemon=True).start()

    def list_directory(self, folder_id=None, page=1):
        xbmcplugin.setContent(self.handle, 'videos')
        started = time.time()
        at_root = folder_id in (None, '', '0')
        MAX_SORT_PAGES = 20
        try:
            folders = self._get_all_folders()
            if page == 1:
                self.add_new_folder_item(str(folder_id) if folder_id else None)
                target_parent = str(folder_id) if folder_id else None
                visible_folders = [
                    f for f in folders if self._parent_id(f) == target_parent
                ]
                visible_folders = self._sort_folders(visible_folders)
                for f in visible_folders:
                    self.add_folder_item(f, context_folder_id=folder_id)
            current_folder_id = str(folder_id) if folder_id else None
            current_folder_name = (self._folder_name_by_id(folder_id)
                                   if folder_id else None)
            all_files = []
            seen_ids = set()
            fetch_page = 1
            pages_fetched = 0
            while fetch_page <= MAX_SORT_PAGES:
                response = (self.api.get_folder_files(folder_id, fetch_page)
                            if folder_id
                            else self.api.get_files(fetch_page))
                batch = response.get('data') or []
                if not batch:
                    break
                pages_fetched += 1
                new_items = 0
                for item in batch:
                    item_id = item.get('id')
                    if item_id is not None:
                        if item_id in seen_ids:
                            continue
                        seen_ids.add(item_id)
                    all_files.append(item)
                    new_items += 1
                if new_items == 0:
                    log(f"Pagination stalled at page {fetch_page}", xbmc.LOGWARNING)
                    break
                if not self._has_next_page(response, fetch_page):
                    break
                fetch_page += 1
            log(f"Fetched {pages_fetched} page(s), {len(all_files)} unique",
                xbmc.LOGDEBUG)
            all_files = self._sort_files(all_files, by_date_desc=at_root)
            start = (page - 1) * PER_PAGE
            files = all_files[start:start + PER_PAGE]
            has_next = (start + PER_PAGE) < len(all_files)
            to_fetch = self._files_needing_fetch(files)
            if to_fetch:
                self._schedule_thumbnail_prefetch(
                    to_fetch, context_folder_id=folder_id
                )
            if time.time() - started > DOS_WARN_SECONDS:
                notify("Filester", "Service slow — may be under load",
                       xbmcgui.NOTIFICATION_WARNING)
            for file in files:
                self.add_file_item(file,
                                    current_folder_name=current_folder_name,
                                    current_folder_id=current_folder_id)
            if has_next:
                extra = {'folder_id': folder_id} if folder_id else {}
                self.add_next_page_item('list', page, **extra)
            xbmcplugin.endOfDirectory(self.handle, succeeded=True)
        except Exception as e:
            log(f"Listing failed: {e}\n{traceback.format_exc()}", xbmc.LOGERROR)
            notify("Filester", f"Folder Error: {str(e)[:40]}",
                   xbmcgui.NOTIFICATION_ERROR)
            xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    def play_file(self, file_id):
        if not file_id:
            notify("Filester", "Missing file id", xbmcgui.NOTIFICATION_ERROR)
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
            return
        try:
            stream_url = self.api.get_stream_url(file_id)
            if not stream_url:
                raise ValueError("Could not generate stream URL")
            item = xbmcgui.ListItem(path=stream_url)
            item.setProperty("IsPlayable", "true")
            xbmcplugin.setResolvedUrl(self.handle, True, item)
        except Exception as e:
            log(f"Playback error: {e}", xbmc.LOGERROR)
            notify("Playback Error", str(e)[:50], xbmcgui.NOTIFICATION_ERROR)
            xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())

    def _prompt_folder_name(self, title, initial=""):
        name = xbmcgui.Dialog().input(
            title, defaultt=initial, type=xbmcgui.INPUT_ALPHANUM
        )
        if not name:
            return None
        name = name.strip()
        if not name:
            return None
        if len(name) > MAX_FOLDER_NAME_LEN:
            notify("Filester",
                   f"Name too long (max {MAX_FOLDER_NAME_LEN} chars)",
                   xbmcgui.NOTIFICATION_ERROR)
            return None
        return name

    def create_folder_prompt(self, parent_id):
        title = "New Subfolder" if parent_id else "New Folder"
        name = self._prompt_folder_name(title)
        if not name:
            return
        new_id = self.api.create_folder(name, parent_id=parent_id)
        if new_id:
            self._invalidate_folder_list()
            notify("Filester", f"Folder '{name}' created")
            refresh_container()
        else:
            notify("Filester", "Failed to create folder",
                   xbmcgui.NOTIFICATION_ERROR)

    def refresh_folder(self, folder_id):
        if not folder_id:
            return
        self.api.invalidate_folder_files(folder_id)
        self.api.invalidate_folder_thumb(str(folder_id))
        self._invalidate_folder_list()
        session_clear_thumb(str(folder_id))
        notify("Filester", "Folder refreshed")
        refresh_container()

    def set_folder_thumbnail(self, folder_id):
        if not folder_id:
            return
        image_path = xbmcgui.Dialog().browse(
            2, "Select an image for the folder thumbnail", 'files',
            '.jpg|.jpeg|.png|.gif|.webp', False, False,
        )
        if not image_path:
            return
        lower = image_path.lower()
        if not lower.endswith(ALLOWED_IMAGE_EXTS):
            notify("Filester", "Please choose a JPG, PNG, GIF or WebP",
                   xbmcgui.NOTIFICATION_ERROR)
            return
        try:
            size = xbmcvfs.Stat(image_path).st_size()
        except Exception:
            size = 0
        if size and size > MAX_THUMB_SIZE:
            notify("Filester", "Image is larger than 5 MB",
                   xbmcgui.NOTIFICATION_ERROR)
            return
        notify("Filester", "Uploading thumbnail…")
        url = self.api.upload_folder_thumbnail(folder_id, image_path)
        if not url:
            notify("Filester", "Failed to upload thumbnail",
                   xbmcgui.NOTIFICATION_ERROR)
            return
        log(f"Uploaded folder thumb for {folder_id}: {url}")
        session_clear_thumb(str(folder_id))
        self.api.invalidate_folder_thumb(str(folder_id))
        self.api.cache_folder_thumb(str(folder_id), url)
        self.api.invalidate_folders_cache()
        self._invalidate_folder_list()
        notify("Filester", "Folder thumbnail updated")
        refresh_container()

    def delete_folder(self, folder_id):
        if not folder_id:
            return
        if not xbmcgui.Dialog().yesno(
            "Filester", "Delete this folder and all its contents?"
        ):
            return
        if self.api.delete_folder(folder_id):
            self._invalidate_folder_list()
            session_clear_thumb(str(folder_id))
            notify("Filester", "Folder deleted")
            refresh_container()
        else:
            notify("Filester", "Failed to delete folder",
                   xbmcgui.NOTIFICATION_ERROR)

    def delete_file(self, file_id):
        if not file_id:
            return
        if not xbmcgui.Dialog().yesno("Filester", "Delete this file?"):
            return
        if self.api.delete_file(file_id):
            notify("Filester", "File deleted")
            refresh_container()
        else:
            notify("Filester", "Failed to delete file",
                   xbmcgui.NOTIFICATION_ERROR)

    def move_to_new_folder_prompt(self, file_id, parent_id=None):
        if not file_id:
            return
        name = self._prompt_folder_name("New Folder")
        if not name:
            return
        new_id = self.api.create_folder_and_move(
            name, file_id, parent_id=parent_id
        )
        if new_id:
            self._invalidate_folder_list()
            notify("Filester", f"Moved to '{name}'")
            refresh_container()
        else:
            notify("Filester", "Failed to create folder and move file",
                   xbmcgui.NOTIFICATION_ERROR)

    def move_file_prompt(self, file_id):
        if not file_id:
            return
        folders = self._get_all_folders()
        if not folders:
            notify("Filester", "No folders available",
                   xbmcgui.NOTIFICATION_ERROR)
            return
        tree = self._build_folder_tree(folders)
        display = ["[Root]"] + [name for _, name in tree]
        ids = [""] + [fid for fid, _ in tree]
        idx = xbmcgui.Dialog().select("Move to folder", display)
        if idx < 0:
            return
        target = ids[idx] or None
        if self.api.move_file(file_id, target):
            notify("Filester", "File moved")
            refresh_container()
        else:
            notify("Filester", "Failed to move file",
                   xbmcgui.NOTIFICATION_ERROR)

    def move_folder_prompt(self, folder_id):
        if not folder_id:
            return
        folders = self._get_all_folders()
        if not folders:
            notify("Filester", "No folders available",
                   xbmcgui.NOTIFICATION_ERROR)
            return
        excluded = self._descendant_ids(str(folder_id))
        tree = [(fid, name) for fid, name in self._build_folder_tree(folders)
                if fid not in excluded]
        display = ["[Root]"] + [name for _, name in tree]
        ids = [""] + [fid for fid, _ in tree]
        idx = xbmcgui.Dialog().select("Move folder to", display)
        if idx < 0:
            return
        target = ids[idx] or None
        if self.api.move_folder(folder_id, target):
            self._invalidate_folder_list()
            notify("Filester", "Folder moved")
            refresh_container()
        else:
            notify("Filester", "Failed to move folder",
                   xbmcgui.NOTIFICATION_ERROR)


# ---------------------------------------------------------------------------
# Reset settings
# ---------------------------------------------------------------------------
def reset_settings() -> None:
    ADDON.setSetting('api_key', '')
    ADDON.setSetting('cache_enabled', 'true')
    ADDON.setSetting('cache_expiry_unlimited', 'false')
    ADDON.setSetting('cache_expiry', '60')
    ADDON.setSetting('default_view', '640')
    ADDON.setSetting('show_new_folder_item', 'false')
    notify("Filester", "Settings reset")


# ---------------------------------------------------------------------------
# Background pre-fetch
# ---------------------------------------------------------------------------
def background_prefetch(api: FilesterAPI) -> None:
    if not api.api_key or not api.cache_enabled:
        return
    try:
        api.get_folders()
        api.get_files(page=1)
    except Exception as e:
        log(f"Background prefetch error: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
LIST_ACTIONS = {None, "", "list"}


def main() -> None:
    api = FilesterAPI()
    params = dict(urllib.parse.parse_qsl(sys.argv[2][1:])) if len(sys.argv) > 2 else {}
    action = params.get("action")
    mode = params.get("mode")
    if mode == "reset_settings":
        reset_settings()
        return
    if action in LIST_ACTIONS and api.cache_enabled:
        threading.Thread(
            target=background_prefetch, args=(api,), daemon=True
        ).start()
    handler = DirectoryHandler(api)
    try:
        if action == "play":
            handler.play_file(params.get("id"))
        elif action == "clear_cache":
            ok, count = api.clear_all_caches()
            if ok:
                notify("Filester", f"Cache cleared ({count} files)")
                refresh_container()
            else:
                notify("Filester", "Failed to clear cache",
                       xbmcgui.NOTIFICATION_ERROR)
        elif action == "create_folder":
            handler.create_folder_prompt(params.get("parent_id") or None)
        elif action == "refresh_folder":
            handler.refresh_folder(params.get("folder_id"))
        elif action == "set_folder_thumbnail":
            handler.set_folder_thumbnail(params.get("folder_id"))
        elif action == "delete_file":
            handler.delete_file(params.get("file_id"))
        elif action == "delete_folder":
            handler.delete_folder(params.get("folder_id"))
        elif action == "move_to_new_folder":
            handler.move_to_new_folder_prompt(
                params.get("file_id"),
                parent_id=params.get("parent_id") or None,
            )
        elif action == "move_file_prompt":
            handler.move_file_prompt(params.get("file_id"))
        elif action == "move_folder_prompt":
            handler.move_folder_prompt(params.get("folder_id"))
        else:
            handler.list_directory(
                folder_id=params.get("folder_id"),
                page=int(params.get("page", 1) or 1),
            )
    except Exception as e:
        log(f"Unhandled error in action '{action}': {e}\n{traceback.format_exc()}",
            xbmc.LOGERROR)
        notify("Filester", str(e)[:60], xbmcgui.NOTIFICATION_ERROR)
        if action in LIST_ACTIONS:
            xbmcplugin.endOfDirectory(handler.handle, succeeded=False)


if __name__ == '__main__':
    main()