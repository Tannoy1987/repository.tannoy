import os
import re
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
DOS_WARN_SECONDS = 5.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 8.0
MAX_FOLDER_NAME_LEN = 100

LISTING_REVALIDATE_SECONDS = 30

ALLOWED_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
MAX_THUMB_SIZE = 5 * 1024 * 1024  # 5 MB

SESSION_THUMB_PREFIX = "Filester.FolderThumb."

PLACEHOLDER_THUMB_PATHS = ('/img/meta.png',)


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


def jittered_backoff(attempt: int, retry_after: Optional[float] = None) -> float:
    if retry_after is not None:
        return min(max(retry_after, 0.5), BACKOFF_MAX)
    base = min(BACKOFF_BASE ** attempt, BACKOFF_MAX)
    return base * (0.5 + random.random() * 0.5)


def is_placeholder_thumb(url: Optional[str]) -> bool:
    if not url:
        return False
    path = url.split('?', 1)[0].rstrip('/')
    return path.endswith(PLACEHOLDER_THUMB_PATHS)


def inline_thumbnail(item: Dict[str, Any]) -> Optional[str]:
    thumb = item.get('thumbnail_url')
    if thumb:
        if thumb.startswith('http'):
            resolved = thumb
        elif thumb.startswith('/'):
            resolved = f"{SITE_BASE}{thumb}"
        else:
            resolved = f"{SITE_BASE}/{thumb}"
        if not is_placeholder_thumb(resolved):
            return resolved

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
    """Return an absolute thumbnail URL from a folder object, checking a
    range of plausible field names. Returns None if nothing is found."""
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
        """Return (age_seconds, value) ignoring TTL, or None if missing."""
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
        """Return (age, data) for cached payload, ignoring TTL."""
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

    def _auth_headers(self) -> Dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def request(
        self,
        endpoint: str,
        method: str = 'GET',
        data: Optional[Dict[str, Any]] = None,
        retries: int = MAX_RETRIES,
    ) -> Dict[str, Any]:
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

    def request_swr(
        self,
        endpoint: str,
        method: str = 'GET',
        data: Optional[Dict[str, Any]] = None,
        retries: int = MAX_RETRIES,
        revalidate_seconds: int = LISTING_REVALIDATE_SECONDS,
    ) -> Dict[str, Any]:
        """Stale-while-revalidate: return cached immediately; if stale,
        revalidate in the background and refresh the container if changed."""
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

    def _schedule_revalidation(
        self, url: str, method: str,
        data: Optional[Dict[str, Any]], retries: int,
        old_data: Dict[str, Any],
    ) -> None:
        with self._reval_lock:
            if url in self._revalidating:
                return
            self._revalidating.add(url)

        def worker() -> None:
            try:
                fresh = self._http_call(url, method, data, retries)
                if fresh != old_data:
                    self._store_caches(url, fresh)
                    log(f"SWR: content changed for {url} — refreshing")
                    xbmc.executebuiltin("Container.Refresh")
            except Exception as e:
                log(f"SWR revalidation failed for {url}: {e}", xbmc.LOGWARNING)
            finally:
                with self._reval_lock:
                    self._revalidating.discard(url)

        threading.Thread(target=worker, daemon=True).start()

    def _http_call(
        self,
        url: str,
        method: str,
        data: Optional[Dict[str, Any]],
        retries: int,
    ) -> Dict[str, Any]:
        if retries <= 0:
            raise RuntimeError(f"request({url}) called with retries <= 0")

        last_error: Optional[Exception] = None

        for attempt in range(retries):
            retry_after: Optional[float] = None
            try:
                headers = self._auth_headers()
                body: Optional[bytes] = None
                if method in ('POST', 'PUT', 'PATCH') and data is not None:
                    body = json.dumps(data).encode('utf-8')
                    headers["Content-Type"] = "application/json"

                req = urllib.request.Request(
                    url, data=body, headers=headers, method=method
                )

                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    raw = resp.read().decode('utf-8')

                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as e:
                    last_error = e
                    log(f"Invalid JSON from {url} (attempt {attempt + 1}/{retries})",
                        xbmc.LOGWARNING)
                else:
                    if not payload.get('success', True):
                        raise Exception(
                            f"API Error: {payload.get('message', 'Unknown')}"
                        )
                    return payload

            except urllib.error.HTTPError as e:
                if e.code in self._NON_RETRYABLE:
                    log(f"HTTP {e.code} on {url} — not retrying", xbmc.LOGERROR)
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

            except (urllib.error.URLError, ConnectionResetError, TimeoutError) as e:
                last_error = e
                log(f"Network error (attempt {attempt + 1}/{retries}): {e}",
                    xbmc.LOGWARNING)

            except Exception:
                raise

            if attempt < retries - 1:
                time.sleep(jittered_backoff(attempt, retry_after))

        if last_error is None:
            last_error = RuntimeError(f"request({url}) failed with no captured error")
        raise last_error

    def get_folders(self) -> List[Dict[str, Any]]:
        return self.request_swr("/folders").get('data', []) or []

    def get_files(self, page: int = 1,
                  folder_id: Optional[str] = None) -> Dict[str, Any]:
        params = {'page': page, 'per_page': PER_PAGE}
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
            log(f"Failed to fetch details for {file_id}: {e}", xbmc.LOGWARNING)
            return {}

        if details and self.cache_enabled:
            self._file_details_cache.set(file_id, details)
        return details

    def cached_thumbnail(self, file_id: str) -> Tuple[bool, Optional[str]]:
        if not self.cache_enabled:
            return (False, None)
        val = self._thumbnail_cache.get(file_id, self.cache_expiry)
        if val is None:
            return (False, None)
        if val == self._THUMB_MISSING:
            return (True, None)
        return (True, val)

    def thumbnail_for_file(self, file_id: str) -> Optional[str]:
        known, url = self.cached_thumbnail(file_id)
        if known:
            return url

        details = self.get_file_details(file_id)
        inline = inline_thumbnail(details)
        url = inline if inline else self._THUMB_MISSING
        if self.cache_enabled:
            self._thumbnail_cache.set(file_id, url)
        return url or None

    def fetch_public_folder_thumb(self, folder_id: str) -> Optional[str]:
        """Fetch the public folder page and extract the og:image thumbnail
        URL. Works only for public folders; returns None otherwise."""
        url = f"{SITE_BASE}/f/{quote_param(folder_id)}"
        headers = {
            "User-Agent": WEB_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                html = resp.read().decode('utf-8', errors='replace')

            # <meta property="og:image" content="...">  (attribute order may vary)
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+'
                r'content=["\']([^"\']+)["\']',
                html, re.IGNORECASE
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
                    r'property=["\']og:image["\']',
                    html, re.IGNORECASE
                )
            if m:
                thumb = m.group(1)
                resolved = absolute_thumb_url(thumb)
                if is_placeholder_thumb(resolved):
                    log(f"Ignoring placeholder og:image for {folder_id}")
                    return None
                log(f"Public folder thumb for {folder_id}: {resolved}")
                return resolved

            log(f"No og:image found on public page for {folder_id}",
                xbmc.LOGDEBUG)
        except urllib.error.HTTPError as e:
            log(f"Public folder page HTTP {e.code} for {folder_id}",
                xbmc.LOGWARNING)
        except Exception as e:
            log(f"Public folder thumb fetch failed for {folder_id}: {e}",
                xbmc.LOGWARNING)
        return None

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
                log("Upload succeeded but no thumbnail_url in response",
                    xbmc.LOGERROR)
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
                raise ValueError("Missing file path or token in API response")

            token = urllib.parse.quote(token_raw, safe='')
            url = f"{server}/v2/{file_path}?token={token}"
            return f"{url}|User-Agent={USER_AGENT}&Connection=keep-alive"

        except Exception as e:
            log(f"Failed to build stream URL for {file_id}: {e}", xbmc.LOGERROR)
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
            self.request(
                f"{API_ROOT}/file/delete",
                method='POST',
                data={"identifiers": [file_id]},
            )
            return True
        except Exception as e:
            log(f"Delete file {file_id} failed: {e}", xbmc.LOGERROR)
            return False

    def delete_folder(self, folder_id: str) -> bool:
        try:
            self.request(
                f"{API_ROOT}/folder/delete",
                method='POST',
                data={"identifiers": [folder_id]},
            )
            self.invalidate_folders_cache()
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

    def create_folder_and_move(self, name: str, file_id: str,
                                parent_id: Optional[str] = None) -> Optional[str]:
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
                            log(f"Failed to delete cache file {name}: {e}")
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
        self._folder_cache_time: float = 0.0

    def _apply_view_mode(self) -> None:
        try:
            view_id = (ADDON.getSetting('default_view') or '').strip()
            if view_id and view_id != '0':
                xbmc.sleep(100)
                xbmc.executebuiltin(f"Container.SetViewMode({view_id})")
        except Exception as e:
            log(f"View mode set failed: {e}")

    @staticmethod
    def _parent_id(folder: Dict[str, Any]) -> Optional[str]:
        parent = folder.get('parent')
        if parent in (None, '', 0, '0'):
            return None
        return str(parent)

    @staticmethod
    def _folder_id(folder: Dict[str, Any]) -> Optional[str]:
        fid = folder.get('id')
        return str(fid) if fid is not None else None

    def _get_all_folders(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        if force_refresh:
            self.api.invalidate_folders_cache()
        try:
            self._all_folders = self.api.get_folders()
        except Exception as e:
            log(f"get_folders failed: {e}", xbmc.LOGERROR)
            if self._all_folders is not None:
                log("Using stale folder list after failure", xbmc.LOGWARNING)
                return self._all_folders
            raise
        return self._all_folders

    def _invalidate_folder_list(self) -> None:
        self._all_folders = None
        self._folder_cache_time = 0.0

    def _folder_name_by_id(self, folder_id: Any) -> Optional[str]:
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

    def _build_folder_tree(
        self,
        folders: List[Dict[str, Any]],
        parent_id: Optional[str] = None,
        level: int = 0,
    ) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
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

    def _thumb_for(self, item: Dict[str, Any]) -> Optional[str]:
        inline = inline_thumbnail(item)
        if inline:
            return inline

        file_id = item.get('id')
        if not file_id:
            return None

        known, url = self.api.cached_thumbnail(str(file_id))
        return url if known else None

    def _get_folder_thumbnail(self, folder_id: str) -> Optional[str]:
        """Resolve a folder thumbnail from Filester.

        Priority:
          1. Session in-memory cache (within this Kodi run)
          2. thumbnail field on the folder object from the API (if ever added)
          3. Public folder page og:image meta tag
        """
        fid = str(folder_id)

        cached = session_get_thumb(fid)
        if cached:
            log(f"Thumb for {fid}: session cache")
            return cached

        for f in self._get_all_folders():
            if self._folder_id(f) != fid:
                continue
            api_thumb = extract_folder_thumb(f)
            if api_thumb:
                log(f"Thumb for {fid}: API folder field -> {api_thumb}")
                session_set_thumb(fid, api_thumb)
                return api_thumb
            break

        web_thumb = self.api.fetch_public_folder_thumb(fid)
        if web_thumb:
            session_set_thumb(fid, web_thumb)
            return web_thumb

        log(f"Thumb for {fid}: none found")
        return None

    def add_new_folder_item(self, parent_id: Optional[str]) -> None:
        if not setting_bool('show_new_folder_item', default=False):
            return

        label = "[B]➕ New Subfolder[/B]" if parent_id else "[B]➕ New Folder[/B]"
        item = xbmcgui.ListItem(label=label)
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle(label)
        info_tag.setMediaType('video')

        item.setArt({'icon': self.NEW_FOLDER_ICON, 'thumb': self.NEW_FOLDER_ICON})

        params: Dict[str, Any] = {'action': 'create_folder'}
        if parent_id:
            params['parent_id'] = parent_id

        xbmcplugin.addDirectoryItem(
            handle=self.handle,
            url=build_url(params),
            listitem=item,
            isFolder=False,
        )

    def add_folder_item(self, folder: Dict[str, Any]) -> None:
        folder_id = self._folder_id(folder)
        if folder_id is None:
            return
        name = folder.get('name', 'Unnamed Folder')

        item = xbmcgui.ListItem(label=name)
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle(name)
        info_tag.setMediaType('video')

        thumb = self._get_folder_thumbnail(folder_id)
        log(f"Folder item {folder_id} '{name}' -> "
            f"thumb={'set' if thumb else 'default'}")
        if thumb:
            item.setArt({
                'thumb': thumb,
                'poster': thumb,
                'icon': thumb,
                'banner': thumb,
            })
        else:
            item.setArt({'icon': self.FOLDER_ICON, 'thumb': self.FOLDER_ICON})

        item.addContextMenuItems([
            ("Set Thumbnail…",
             f"RunPlugin({build_url({'action': 'set_folder_thumbnail', 'folder_id': folder_id})})"),
            ("Refresh Folder",
             f"RunPlugin({build_url({'action': 'refresh_folder', 'folder_id': folder_id})})"),
            ("New Subfolder",
             f"RunPlugin({build_url({'action': 'create_folder', 'parent_id': folder_id})})"),
            ("Delete Folder",
             f"RunPlugin({build_url({'action': 'delete_folder', 'folder_id': folder_id})})"),
        ], replaceItems=False)

        xbmcplugin.addDirectoryItem(
            handle=self.handle,
            url=build_url({'action': 'list', 'folder_id': folder_id}),
            listitem=item,
            isFolder=True,
        )

    def add_file_item(self, file: Dict[str, Any],
                      current_folder_name: Optional[str] = None,
                      current_folder_id: Optional[str] = None) -> None:
        name = file.get('name', 'Unknown')
        file_id = file.get('id')

        file_folder_id = file.get('folder_id')
        folder_label: Optional[str] = None
        if file_folder_id:
            folder_label = self._folder_name_by_id(file_folder_id)

        item = xbmcgui.ListItem(label=name)
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle(name)
        info_tag.setMediaType('video')

        item.setProperty("IsPlayable", "true")

        if folder_label:
            item.setProperty(self.PROP_FOLDER_NAME, folder_label)

        thumb = self._thumb_for(file)
        if thumb:
            item.setArt({'thumb': thumb, 'poster': thumb, 'icon': thumb})
        else:
            item.setArt({'icon': self.VIDEO_ICON})

        menu: List[Tuple[str, str]] = []

        if file_folder_id and str(file_folder_id) != str(current_folder_id or ''):
            menu.append((
                "Go to Folder",
                f"Container.Update({build_url({'action': 'list', 'folder_id': file_folder_id})})",
            ))

        menu.append((
            "Move to Folder…",
            f"RunPlugin({build_url({'action': 'move_file_prompt', 'file_id': file_id})})",
        ))

        new_folder_params: Dict[str, Any] = {
            'action': 'move_to_new_folder',
            'file_id': file_id,
        }
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
            listitem=item,
            isFolder=False,
        )

    def add_next_page_item(self, action: str, page: int, **extra: Any) -> None:
        item = xbmcgui.ListItem(label="[B]Next Page >>[/B]")
        info_tag = item.getVideoInfoTag()
        info_tag.setTitle("Next Page")
        info_tag.setMediaType('video')

        params = {'action': action, 'page': page + 1, **extra}
        xbmcplugin.addDirectoryItem(
            handle=self.handle,
            url=build_url(params),
            listitem=item,
            isFolder=True,
        )

    @staticmethod
    def _has_next_page(response: Dict[str, Any], page: int = 1) -> bool:
        pagination = response.get('pagination') or {}
        if pagination:
            current = pagination.get('page') or page
            total = pagination.get('pages') or pagination.get('last_page') or 1
            return current < total
        files = response.get('data') or []
        return len(files) >= PER_PAGE

    def _files_needing_fetch(self, files: List[Dict[str, Any]]) -> List[str]:
        out: List[str] = []
        for f in files:
            fid = f.get('id')
            if not fid:
                continue
            if inline_thumbnail(f):
                continue
            known, _ = self.api.cached_thumbnail(str(fid))
            if not known:
                out.append(str(fid))
        return out

    def _prefetch_thumbnails(self, file_ids: List[str]) -> None:
        if not file_ids:
            return

        total = len(file_ids)
        done = 0
        cancel = threading.Event()

        progress = xbmcgui.DialogProgress()
        progress.create("Filester", "Loading thumbnails…")

        def worker(file_id: str) -> None:
            if cancel.is_set():
                return
            try:
                self.api.thumbnail_for_file(file_id)
            except Exception as e:
                log(f"Thumbnail prefetch failed for {file_id}: {e}",
                    xbmc.LOGWARNING)

        pool = ThreadPoolExecutor(max_workers=THUMB_FETCH_WORKERS)
        try:
            futures = [pool.submit(worker, fid) for fid in file_ids]
            for fut in as_completed(futures):
                done += 1
                if progress.iscanceled():
                    cancel.set()
                    break
                pct = int(done / total * 100)
                progress.update(pct, f"Loading thumbnails ({done}/{total})")
                try:
                    fut.result()
                except Exception:
                    pass
        finally:
            pool.shutdown(wait=False)
            try:
                progress.close()
            except Exception:
                pass

    def list_directory(self, folder_id: Optional[str] = None, page: int = 1) -> None:
        xbmcplugin.setContent(self.handle, 'videos')
        started = time.time()

        try:
            folders = self._get_all_folders()

            if page == 1:
                self.add_new_folder_item(str(folder_id) if folder_id else None)

                target_parent = str(folder_id) if folder_id else None
                for f in folders:
                    if self._parent_id(f) == target_parent:
                        self.add_folder_item(f)

            current_folder_id = str(folder_id) if folder_id else None
            current_folder_name = (self._folder_name_by_id(folder_id)
                                   if folder_id else None)

            response = (self.api.get_folder_files(folder_id, page)
                        if folder_id else self.api.get_files(page))
            files = response.get('data', [])

            to_fetch = self._files_needing_fetch(files)
            if to_fetch:
                self._prefetch_thumbnails(to_fetch)

            if time.time() - started > DOS_WARN_SECONDS:
                notify("Filester", "Service slow — may be under load",
                       xbmcgui.NOTIFICATION_WARNING)

            for file in files:
                self.add_file_item(
                    file,
                    current_folder_name=current_folder_name,
                    current_folder_id=current_folder_id,
                )

            if self._has_next_page(response, page):
                extra = {'folder_id': folder_id} if folder_id else {}
                self.add_next_page_item('list', page, **extra)

            xbmcplugin.endOfDirectory(self.handle, succeeded=True)
            self._apply_view_mode()

        except Exception as e:
            log(f"Listing failed: {e}\n{traceback.format_exc()}", xbmc.LOGERROR)
            notify("Filester", f"Folder Error: {str(e)[:40]}",
                   xbmcgui.NOTIFICATION_ERROR)
            xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    def play_file(self, file_id: Optional[str]) -> None:
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

    def _prompt_folder_name(self, title: str,
                            initial: str = "") -> Optional[str]:
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

    def create_folder_prompt(self, parent_id: Optional[str]) -> None:
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

    def refresh_folder(self, folder_id: Optional[str]) -> None:
        if not folder_id:
            return
        self.api.invalidate_folder_files(folder_id)
        self._invalidate_folder_list()
        session_clear_thumb(str(folder_id))
        notify("Filester", "Folder refreshed")
        refresh_container()

    def set_folder_thumbnail(self, folder_id: Optional[str]) -> None:
        if not folder_id:
            return

        image_path = xbmcgui.Dialog().browse(
            2,
            "Select an image for the folder thumbnail",
            'files',
            '.jpg|.jpeg|.png|.gif|.webp',
            False,
            False,
        )
        if not image_path:
            return

        lower = image_path.lower()
        if not lower.endswith(ALLOWED_IMAGE_EXTS):
            notify("Filester", "Please choose a JPG, PNG, GIF or WebP image",
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

        # Clear any cached thumb for this folder so the new one is fetched
        session_clear_thumb(str(folder_id))
        self.api.invalidate_folders_cache()
        self._invalidate_folder_list()

        notify("Filester", "Folder thumbnail updated")
        refresh_container()

    def delete_folder(self, folder_id: Optional[str]) -> None:
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

    def delete_file(self, file_id: Optional[str]) -> None:
        if not file_id:
            return
        if not xbmcgui.Dialog().yesno("Filester", "Delete this file?"):
            return
        if self.api.delete_file(file_id):
            notify("Filester", "File deleted")
            refresh_container()
        else:
            notify("Filester", "Failed to delete file", xbmcgui.NOTIFICATION_ERROR)

    def move_to_new_folder_prompt(self, file_id: Optional[str],
                                  parent_id: Optional[str] = None) -> None:
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

    def move_file_prompt(self, file_id: Optional[str]) -> None:
        if not file_id:
            return

        folders = self._get_all_folders()
        if not folders:
            notify("Filester", "No folders available", xbmcgui.NOTIFICATION_ERROR)
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
            notify("Filester", "Failed to move file", xbmcgui.NOTIFICATION_ERROR)


# ---------------------------------------------------------------------------
# Reset settings helper
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