"""Filester API client for Kodi.

Implements every endpoint in the Filester API reference, including all
documented quirks and workarounds. Thumbnail caches are two-tier:
session-scoped Window(10000) properties (fast) backed by a persistent
disk JSON file (survives Kodi restart).

No third-party dependencies: stdlib only (urllib + ssl + json).
"""

import json
import mimetypes
import random
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

try:
    import xbmc as _xbmc
    import xbmcvfs as _xbmcvfs
    import xbmcaddon as _xbmcaddon
    LOGDEBUG = _xbmc.LOGDEBUG
    LOGINFO = _xbmc.LOGINFO
    LOGWARNING = _xbmc.LOGWARNING
    LOGERROR = _xbmc.LOGERROR

    def _log(msg, level=LOGDEBUG):
        try:
            _xbmc.log("[Filester] " + str(msg), level)
        except Exception:
            print("[Filester] " + str(msg))
except Exception:
    _xbmc = None
    _xbmcvfs = None
    _xbmcaddon = None
    LOGDEBUG = LOGINFO = LOGWARNING = LOGERROR = 0

    def _log(msg, level=0):
        print("[Filester] " + str(msg))

try:
    import xbmcgui as _gui
except Exception:
    _gui = None

API_V1 = "https://u1.filester.me/api/v1"
API_ROOT = "https://u1.filester.me"
PUBLIC_VIEW = "https://filester.me/v2/api/public/view"
FOLDER_PAGE = "https://filester.me/f/{folder_id}"
SITE_BASE = "https://filester.me"

DEFAULT_UA = "Kodi/20.0 (Windows NT 10.0; Win64; x64)"
PUBLIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

THUMB_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
THUMB_MAX_BYTES = 5 * 1024 * 1024

_THUMB_SEMAPHORE = threading.Semaphore(10)


def _prop_get(key):
    if _gui is None:
        return ""
    try:
        return _gui.Window(10000).getProperty(key) or ""
    except Exception:
        return ""


def _prop_set(key, value):
    if _gui is None:
        return
    try:
        _gui.Window(10000).setProperty(key, str(value))
    except Exception:
        pass


_CACHE_DIR = None
_DISK_CACHE = None
_DISK_CACHE_PATH = None
_DISK_CACHE_LOADED = False
_DISK_CACHE_LOCK = threading.Lock()
_DISK_CACHE_DIRTY = False
_DISK_CACHE_LAST_WRITE = 0.0
_DISK_CACHE_TTL = 30 * 86400
_DISK_CACHE_FLUSH_INTERVAL = 3.0


def _cache_dir():
    global _CACHE_DIR
    if _CACHE_DIR is not None:
        return _CACHE_DIR
    if _xbmcvfs is None or _xbmcaddon is None:
        _CACHE_DIR = ""
        return _CACHE_DIR
    try:
        addon = _xbmcaddon.Addon()
        profile = _xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        if profile and not profile.endswith(('/', '\\')):
            profile += '/'
        if not _xbmcvfs.exists(profile):
            _xbmcvfs.mkdirs(profile)
        _CACHE_DIR = profile
    except Exception:
        _CACHE_DIR = ""
    return _CACHE_DIR


def _load_disk_cache():
    global _DISK_CACHE, _DISK_CACHE_PATH, _DISK_CACHE_LOADED
    if _DISK_CACHE_LOADED:
        return
    _DISK_CACHE_LOADED = True
    _DISK_CACHE = {}
    d = _cache_dir()
    if not d or _xbmcvfs is None:
        return
    _DISK_CACHE_PATH = d + "thumb_cache.json"
    try:
        if _xbmcvfs.exists(_DISK_CACHE_PATH):
            fh = _xbmcvfs.File(_DISK_CACHE_PATH)
            try:
                data = fh.read()
            finally:
                fh.close()
            if data:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    now = time.time()
                    _DISK_CACHE = {
                        k: v for k, v in parsed.items()
                        if isinstance(v, dict)
                        and (now - v.get("ts", 0)) < _DISK_CACHE_TTL
                    }
    except Exception:
        _DISK_CACHE = {}


def _flush_disk_cache(force=False):
    global _DISK_CACHE_DIRTY, _DISK_CACHE_LAST_WRITE
    if not _DISK_CACHE_DIRTY or _DISK_CACHE_PATH is None or _xbmcvfs is None:
        return
    now = time.time()
    if not force and (now - _DISK_CACHE_LAST_WRITE) < _DISK_CACHE_FLUSH_INTERVAL:
        return
    _DISK_CACHE_DIRTY = False
    _DISK_CACHE_LAST_WRITE = now
    try:
        fh = _xbmcvfs.File(_DISK_CACHE_PATH, 'w')
        try:
            fh.write(json.dumps(_DISK_CACHE))
        finally:
            fh.close()
    except Exception:
        _DISK_CACHE_DIRTY = True


def disk_cache_get(key):
    _load_disk_cache()
    with _DISK_CACHE_LOCK:
        entry = _DISK_CACHE.get(str(key))
    if entry and isinstance(entry, dict):
        url = entry.get("url", "")
        ts = entry.get("ts", 0)
        if time.time() - ts < _DISK_CACHE_TTL:
            return url or "__NONE__"
    return None


def disk_cache_set(key, url):
    global _DISK_CACHE_DIRTY
    _load_disk_cache()
    with _DISK_CACHE_LOCK:
        _DISK_CACHE[str(key)] = {"url": url or "", "ts": time.time()}
        _DISK_CACHE_DIRTY = True
    _flush_disk_cache()


def disk_cache_delete(key):
    global _DISK_CACHE_DIRTY
    _load_disk_cache()
    with _DISK_CACHE_LOCK:
        if str(key) in _DISK_CACHE:
            del _DISK_CACHE[str(key)]
            _DISK_CACHE_DIRTY = True
    _flush_disk_cache()


def flush_disk_cache(force=True):
    _flush_disk_cache(force=force)


def _thumb_get(fid):
    if not fid:
        return None
    v = _prop_get("Filester.FileThumb." + str(fid))
    if v:
        return v
    disk = disk_cache_get("file_" + str(fid))
    if disk is not None:
        _prop_set("Filester.FileThumb." + str(fid), disk)
        return disk
    return None


def _thumb_set(fid, url):
    if not fid:
        return
    _prop_set("Filester.FileThumb." + str(fid), url or "__NONE__")
    disk_cache_set("file_" + str(fid), url or "")


def _uuid404_get(uuid):
    if not uuid:
        return False
    if _prop_get("Filester.Uuid404." + str(uuid)):
        return True
    disk = disk_cache_get("uuid404_" + str(uuid))
    if disk is not None:
        _prop_set("Filester.Uuid404." + str(uuid), "1")
        return True
    return False


def _uuid404_set(uuid):
    if not uuid:
        return
    _prop_set("Filester.Uuid404." + str(uuid), "1")
    disk_cache_set("uuid404_" + str(uuid), "1")


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
    re.IGNORECASE,
)


class FilesterError(Exception):
    def __init__(self, message, code=None, transient=False):
        super().__init__(message)
        self.code = code
        self.transient = transient


class RateLimitError(FilesterError):
    pass


class CertError(FilesterError):
    pass


def _is_cert_failure(exc):
    s = "%s %s" % (type(exc).__name__, exc)
    return (
        "CERTIFICATE_VERIFY_FAILED" in s
        or "certificate verify failed" in s.lower()
        or isinstance(exc, ssl.SSLCertVerificationError)
    )


def folder_id_of(folder):
    if isinstance(folder, dict):
        return folder.get("id") or folder.get("identifier")
    return folder


def file_thumb_url(fobj):
    for key in ("thumbnail_url", "thumbnail", "preview_url", "preview"):
        v = (fobj or {}).get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if v.startswith("//"):
                return "https:" + v
            if v.startswith("/"):
                return SITE_BASE + v
            if v.startswith("http"):
                return v
    return ""


def sort_files(files, mode="newest"):
    files = list(files or [])

    def _created(f):
        return str(f.get("created_at") or "")

    def _numid(f):
        try:
            return int(str(f.get("id") or "0"))
        except (ValueError, TypeError):
            return 0

    if mode == "oldest":
        files.sort(key=lambda f: (_created(f), _numid(f)))
    elif mode == "az":
        files.sort(key=lambda f: str(f.get("name") or f.get("original_name") or f.get("id") or "").lower())
    elif mode == "za":
        files.sort(key=lambda f: str(f.get("name") or f.get("original_name") or f.get("id") or "").lower(), reverse=True)
    elif mode == "largest":
        files.sort(key=lambda f: int(f.get("size") or 0), reverse=True)
    elif mode == "smallest":
        files.sort(key=lambda f: int(f.get("size") or 0))
    else:
        files.sort(key=lambda f: (_created(f), _numid(f)), reverse=True)
    return files


def sort_folders(folders, mode="az"):
    folders = list(folders or [])
    folders.sort(key=lambda f: str(f.get("name") or f.get("id") or "").lower(),
                 reverse=(mode == "za"))
    return folders


def descendant_ids(folders, root_id):
    by_parent = {}
    for f in folders or []:
        pid = f.get("parent")
        if pid:
            by_parent.setdefault(str(pid), []).append(f)
    out, stack = set(), [str(root_id)]
    while stack:
        for child in by_parent.get(stack.pop(), []):
            cid = str(folder_id_of(child) or "")
            if cid and cid not in out:
                out.add(cid)
                stack.append(cid)
    return out


def move_exclusions(folders, folder_id):
    return descendant_ids(folders, folder_id) | set([str(folder_id)])


def cached_file_thumb(fobj):
    fid = str((fobj or {}).get("id") or "")
    url = file_thumb_url(fobj or {})
    if url:
        _thumb_set(fid, url)
        return url
    cached = _thumb_get(fid)
    if cached == "__NONE__":
        return ""
    if cached:
        return cached
    if (fobj or {}).get("has_thumbnail") is False:
        _thumb_set(fid, "")
        return ""
    return None


def resolve_file_thumbnail(api, fobj):
    fast = cached_file_thumb(fobj)
    if fast is not None:
        return fast
    fid = str((fobj or {}).get("id") or "")
    if not fid:
        return ""
    try:
        with _THUMB_SEMAPHORE:
            details = api.get_file(fid)
    except Exception:
        return ""
    if not isinstance(details, dict):
        return ""
    url = file_thumb_url(details)
    if url:
        _thumb_set(fid, url)
        return url
    if details.get("has_thumbnail") is False:
        _thumb_set(fid, "")
        return ""
    uuid = details.get("uuid") or (fobj or {}).get("uuid")
    if uuid and not _uuid404_get(uuid):
        probe = api.uuid_thumb_url(uuid)
        if probe and probe != "404":
            _thumb_set(fid, probe)
            return probe
        if probe == "404":
            _uuid404_set(uuid)
        else:
            return ""
    _thumb_set(fid, "")
    return ""


class FilesterAPI:
    def __init__(self, api_key, timeout=25, cache_buster=True,
                 verify_ssl=True, ssl_fallback=True, user_agent=DEFAULT_UA):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.cache_buster = cache_buster
        self.verify_ssl = verify_ssl
        self.ssl_fallback = ssl_fallback
        self.user_agent = user_agent or DEFAULT_UA

    def _ssl_context(self, unverified=False):
        if unverified or not self.verify_ssl:
            return ssl._create_unverified_context()
        return None

    def _request(self, method, url, params=None, json_body=None,
                 raw_bytes=None, content_type=None):
        if params:
            qs = urllib.parse.urlencode(params)
            url = url + ("&" if "?" in url else "?") + qs
        if self.cache_buster and method == "GET" and "_t" not in url:
            sep = "&" if "?" in url else "?"
            url = "%s%s_t=%d" % (url, sep, int(time.time() * 1000))
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html;q=0.8",
        }
        if self.cache_buster:
            headers["Cache-Control"] = "no-cache"
            headers["Pragma"] = "no-cache"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_bytes is not None:
            data = raw_bytes
            if content_type:
                headers["Content-Type"] = content_type
        if method in ("POST", "PUT", "DELETE") and data is None and json_body is None and raw_bytes is None:
            data = b""

        def _do(unverified):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            ctx = self._ssl_context(unverified=unverified)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                    body = resp.read()
                    return resp.status, body
            except urllib.error.HTTPError as e:
                try:
                    ebody = e.read()
                except Exception:
                    ebody = b""
                raise FilesterError(
                    "HTTP %s: %s" % (e.code, ebody[:300].decode("utf-8", "replace")),
                    code=e.code,
                    transient=e.code in (500, 502, 503, 504),
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                reason = getattr(e, "reason", e)
                if _is_cert_failure(e) or _is_cert_failure(reason):
                    raise CertError("CERTIFICATE_VERIFY_FAILED: %s" % e)
                raise FilesterError("Network error: %s" % e, transient=True)

        def _backoff(attempt):
            base = 2.0 ** attempt
            delay = base + random.uniform(-0.5 * base, 0.5 * base)
            time.sleep(max(0.2, delay))

        last_error = None
        for attempt in range(3):
            try:
                try:
                    status, body = _do(unverified=False)
                except CertError as ce:
                    if self.ssl_fallback or not self.verify_ssl:
                        _log("WARNING: %s — retrying once without SSL "
                             "verification (this connection is unprotected)." % ce,
                             LOGWARNING)
                        status, body = _do(unverified=True)
                    else:
                        raise
            except RateLimitError:
                raise
            except FilesterError as e:
                if e.code == 429:
                    raise RateLimitError(str(e) or "HTTP 429 rate limited", code=429)
                last_error = e
                if e.transient and attempt < 2:
                    _backoff(attempt)
                    continue
                raise
            if status == 429:
                raise RateLimitError("HTTP 429: rate limited (~1000 req/hour). Back off for several minutes.", code=429)
            text = body.decode("utf-8", "replace") if body else ""
            return status, text
        raise last_error

    def _get_json(self, url, params=None):
        _status, text = self._request("GET", url, params=params)
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise FilesterError("Bad JSON from %s: %s" % (url, text[:200]))

    def _post_json(self, url, body):
        _status, text = self._request("POST", url, json_body=body)
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise FilesterError("Bad JSON from POST %s: %s" % (url, text[:200]))

    @staticmethod
    def _items(payload):
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            return data if isinstance(data, list) else []
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _pagination(payload):
        if isinstance(payload, dict):
            p = payload.get("pagination") or payload.get("meta") or {}
            if isinstance(p, dict):
                try:
                    return {
                        "page": int(p.get("page", 1)),
                        "pages": int(p.get("pages", 1)),
                        "per_page": int(p.get("per_page", 100)),
                        "total": int(p.get("total", 0)),
                    }
                except (ValueError, TypeError):
                    pass
        return {"page": 1, "pages": 1, "per_page": 100, "total": 0}

    def get_folders(self):
        payload = self._get_json(API_V1 + "/folders")
        return self._items(payload)

    def create_folder(self, name, parent=None):
        body = {"name": name}
        body["parent"] = parent if parent else None
        payload = self._post_json(API_V1 + "/folder", body)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data or payload

    def delete_folders(self, identifiers):
        return self._post_json(API_ROOT + "/folder/delete", {"identifiers": list(identifiers)})

    def move_folder(self, folder_id, new_parent):
        return self._post_json(API_V1 + "/folder/move", {
            "folder": folder_id,
            "parent": new_parent if new_parent else None,
        })

    def upload_folder_thumbnail(self, folder_id, image_path, basename=None):
        with open(image_path, "rb") as fh:
            blob = fh.read()
        if len(blob) > THUMB_MAX_BYTES:
            raise FilesterError("Image too large (max 5 MB).")
        name = basename or image_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        ctype, _enc = mimetypes.guess_type(name)
        if not ctype or not any(name.lower().endswith(e) for e in THUMB_EXTS):
            ctype = "image/jpeg"
        boundary = "FilesterKodi%s" % int(time.time() * 1000)
        crlf = b"\r\n"
        parts = []
        parts.append(("--%s" % boundary).encode() + crlf +
                     b'Content-Disposition: form-data; name="folder"' + crlf + crlf +
                     str(folder_id).encode() + crlf)
        parts.append(("--%s" % boundary).encode() + crlf +
                     ('Content-Disposition: form-data; name="thumbnail"; filename="%s"' % name).encode() + crlf +
                     ("Content-Type: %s" % ctype).encode() + crlf + crlf +
                     blob + crlf)
        parts.append(("--%s--" % boundary).encode() + crlf)
        raw = b"".join(parts)
        _status, text = self._request("POST", API_V1 + "/folder/thumbnail",
                                      raw_bytes=raw,
                                      content_type="multipart/form-data; boundary=%s" % boundary)
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise FilesterError("Bad JSON from thumbnail upload: %s" % text[:200])

    def get_folder_thumbnail(self, folder_id):
        url = FOLDER_PAGE.format(folder_id=urllib.parse.quote(str(folder_id), safe=""))

        def _fetch(unverified):
            req = urllib.request.Request(url, headers={"User-Agent": PUBLIC_UA})
            ctx = self._ssl_context(unverified=unverified)
            try:
                with _THUMB_SEMAPHORE:
                    with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                        return resp.read().decode("utf-8", "replace"), None
            except urllib.error.HTTPError as e:
                return None, e
            except Exception as e:
                if _is_cert_failure(e) or _is_cert_failure(getattr(e, "reason", e)):
                    raise CertError("CERTIFICATE_VERIFY_FAILED: %s" % e)
                raise FilesterError("folder page: %s" % e, transient=True)

        try:
            html, http_err = _fetch(unverified=False)
        except CertError as ce:
            if self.ssl_fallback or not self.verify_ssl:
                _log("WARNING: %s — retrying folder page without SSL "
                     "verification (this connection is unprotected)." % ce,
                     LOGWARNING)
                html, http_err = _fetch(unverified=True)
            else:
                raise
        if http_err is not None:
            if http_err.code == 404:
                return ""
            raise FilesterError("folder page %s" % http_err.code,
                                  code=http_err.code, transient=True)
        m = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE2.search(html)
        if not m:
            return ""
        thumb = m.group(1).strip()
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        elif thumb.startswith("/"):
            thumb = SITE_BASE + thumb
        return thumb

    def get_files(self, page=1, per_page=100, folder=None):
        params = {"page": int(page), "per_page": min(int(per_page), 100)}
        if folder:
            params["folder"] = str(folder)
        payload = self._get_json(API_V1 + "/files", params=params)
        return self._items(payload), self._pagination(payload)

    def get_folder_files(self, folder_id, page=1, per_page=100):
        params = {"page": int(page), "per_page": min(int(per_page), 100)}
        payload = self._get_json(
            API_V1 + "/folder/%s/files" % urllib.parse.quote(str(folder_id), safe=""),
            params=params,
        )
        return self._items(payload), self._pagination(payload)

    def get_all_files(self, folder=None, per_page=100, use_folder_endpoint=False,
                      max_pages=100, progress=None):
        seen = set()
        out = []
        page = 1
        while page <= max_pages:
            if use_folder_endpoint and folder:
                items, _pag = self.get_folder_files(folder, page=page, per_page=per_page)
            else:
                items, pag = self.get_files(page=page, per_page=per_page, folder=folder)
            new_count = 0
            for it in items:
                fid = str(it.get("id", ""))
                if fid and fid not in seen:
                    seen.add(fid)
                    out.append(it)
                    new_count += 1
            if callable(progress):
                try:
                    progress(len(out), pag.get("total", 0) if not (use_folder_endpoint and folder) else 0)
                except Exception:
                    pass
            if not items:
                break
            if use_folder_endpoint and folder:
                if new_count == 0:
                    break
                page += 1
                if page > 5 and new_count == 0:
                    break
            else:
                pages = pag.get("pages", 1) or 1
                if page >= pages:
                    break
                page += 1
        return out

    def get_file(self, file_id):
        payload = self._get_json(
            API_V1 + "/file/%s" % urllib.parse.quote(str(file_id), safe=""))
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def delete_files(self, identifiers):
        return self._post_json(API_ROOT + "/file/delete", {"identifiers": list(identifiers)})

    def move_file(self, file_id, target_folder):
        return self._post_json(API_V1 + "/file/move", {
            "file": str(file_id),
            "folder": target_folder if target_folder else None,
        })

    def uuid_thumb_url(self, uuid):
        url = "https://filester.me/t/%s" % urllib.parse.quote(str(uuid), safe="")

        def _try(method, unverified):
            headers = {"User-Agent": PUBLIC_UA}
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(url, headers=headers, method=method)
            ctx = self._ssl_context(unverified=unverified)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                    ctype = str(resp.headers.get_content_type() or "")
                    if resp.status in (200, 206) and ctype.startswith("image/"):
                        return url
                    return "404" if resp.status == 404 else None
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return "404"
                if e.code in (405, 501) and method == "HEAD":
                    return "retry-get"
                return None
            except Exception as e:
                if _is_cert_failure(e) or _is_cert_failure(getattr(e, "reason", e)):
                    return "retry-unverified"
                return None

        attempts = [False]
        if self.ssl_fallback or not self.verify_ssl:
            attempts.append(True)

        for unverified in attempts:
            if unverified:
                _log("WARNING: CERTIFICATE_VERIFY_FAILED on %s — retrying "
                     "once without verification (this connection is "
                     "unprotected)." % url, LOGWARNING)
            r = _try("HEAD", unverified)
            if r == "retry-get":
                r = _try("GET", unverified)
            if r == "retry-unverified":
                continue
            return r
        return None

    def get_stream_parts(self, file_slug):
        payload = self._post_json(PUBLIC_VIEW, {"file_slug": file_slug})
        data = payload
        if isinstance(payload, dict):
            for key in ("data", "result"):
                if isinstance(payload.get(key), dict):
                    data = payload[key]
                    break
        if not isinstance(data, dict):
            raise FilesterError("Unexpected stream response.")
        server = (data.get("server") or "").rstrip("/")
        fpath = (data.get("file") or "").lstrip("/")
        token = data.get("token") or data.get("access_token") or ""
        if not server or not fpath or not token:
            raise FilesterError("Incomplete stream response (need server/file/token).")
        return server, fpath, token

    def build_stream_url(self, server, fpath, token, stream_ua=None):
        base = "%s/v2/%s?token=%s" % (server.rstrip("/"), fpath.lstrip("/"), token)
        ua = stream_ua or self.user_agent or DEFAULT_UA
        return "%s|User-Agent=%s&Connection=keep-alive" % (
            base, urllib.parse.quote(ua, safe=""))

    def resolve_file(self, file_id, stream_ua=None):
        details = self.get_file(file_id)
        slug = (details or {}).get("slug") if isinstance(details, dict) else None
        if not slug:
            raise FilesterError("missing-slug")
        server, fpath, token = self.get_stream_parts(slug)
        return self.build_stream_url(server, fpath, token, stream_ua), details