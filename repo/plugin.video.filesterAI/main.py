# -*- coding: utf-8 -*-
"""Filester Kodi addon — main entry point."""
import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "resources", "lib"))
from filester_api import (  # noqa: E402
    FilesterAPI, FilesterError, RateLimitError,
    file_thumb_url, cached_file_thumb, resolve_file_thumbnail,
    sort_files, move_exclusions, folder_id_of,
    disk_cache_get, disk_cache_set, disk_cache_delete, flush_disk_cache,
    THUMB_MAX_BYTES,
)

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE_URL = sys.argv[0] if len(sys.argv) > 0 else ""
_ = ADDON.getLocalizedString


def log(msg, level=xbmc.LOGDEBUG):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), level)


# ---------- session + disk thumb cache (folder thumbs) ----------

def _thumb_session_get(fid):
    if not fid:
        return None
    try:
        v = xbmcgui.Window(10000).getProperty("Filester.FolderThumb." + str(fid))
    except Exception:
        v = None
    if v:
        return v
    disk = disk_cache_get("folder_" + str(fid))
    if disk is not None:
        try:
            xbmcgui.Window(10000).setProperty(
                "Filester.FolderThumb." + str(fid), disk)
        except Exception:
            pass
        return disk
    return None


def _thumb_session_set(fid, url):
    if not fid:
        return
    v = url or "__NONE__"
    try:
        xbmcgui.Window(10000).setProperty("Filester.FolderThumb." + str(fid), v)
    except Exception:
        pass
    try:
        disk_cache_set("folder_" + str(fid), url or "")
    except Exception:
        pass


VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts",
              ".m2ts", ".mpg", ".mpeg", ".flv", ".wmv", ".ogv", ".3gp",
              ".iso")


def refresh_limited():
    try:
        prop = xbmcgui.Window(10000).getProperty("Filester.RefreshTimes") or "[]"
        times = json.loads(prop)
    except Exception:
        times = []
    now = time.time()
    times = [t for t in times if isinstance(t, (int, float)) and now - t < 60]
    if len(times) >= 5:
        log("refresh throttled (>5/min), skipping", xbmc.LOGWARNING)
        return False
    times.append(now)
    try:
        xbmcgui.Window(10000).setProperty("Filester.RefreshTimes", json.dumps(times))
    except Exception:
        pass
    xbmc.executebuiltin("Container.Refresh")
    return True


def _invalidate_folder_cache(folder_id):
    try:
        xbmcgui.Window(10000).clearProperty("Filester.FolderThumb." + str(folder_id))
    except Exception:
        pass
    try:
        disk_cache_delete("folder_" + str(folder_id))
    except Exception:
        pass


def _is_video(fobj):
    if str((fobj or {}).get("type") or "").lower().startswith("video"):
        return True
    name = str((fobj or {}).get("name") or (fobj or {}).get("original_name") or "").lower()
    return name.endswith(VIDEO_EXTS)


def cached_folder_thumb(folder):
    fid = str(folder_id_of(folder) or "")
    cached = _thumb_session_get(fid)
    if cached == "__NONE__":
        return ""
    if cached:
        return cached
    for key in ("thumbnail_url", "thumbnail", "cover", "art", "image"):
        v = folder.get(key)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if s.startswith("//"):
                s = "https:" + s
            elif s.startswith("/"):
                s = "https://filester.me" + s
            if s.startswith("http"):
                _thumb_session_set(fid, s)
                return s
    return None


def _note_thumb(fid, url):
    _thumb_session_set(fid, url or "")


def _prefetch_thumbs_sync(missing_folders, missing_files, budget=20):
    show_folders = ADDON.getSetting("show_folder_thumbs") != "false"
    folders = [f for f in (missing_folders or [])
               if show_folders and _thumb_session_get(f) is None]
    files = [f for f in (missing_files or [])
             if cached_file_thumb(f) is None]
    total = len(folders) + len(files)
    if total == 0:
        return

    start_ts = time.time()
    deadline = start_ts + budget
    folder_deadline = start_ts + max(budget * 0.75, 12)
    done = [0]
    failed_folders = [0]
    failed_files = [0]
    cancelled = [False]

    prog = None
    try:
        prog = xbmcgui.DialogProgress()
        prog.create("Filester", "Loading thumbnails…")
    except Exception:
        prog = None

    api = settings_api()
    api.timeout = 5

    def _update():
        if prog:
            try:
                prog.update(int(done[0] * 100 / total),
                            "Thumbnails (%d/%d)" % (done[0], total))
            except Exception:
                pass

    def _should_abort_folder():
        if cancelled[0]:
            return True
        if time.time() > folder_deadline:
            cancelled[0] = True
            return True
        if prog:
            try:
                if prog.iscanceled():
                    cancelled[0] = True
                    return True
            except Exception:
                pass
        return False

    def _should_abort_file():
        if time.time() > deadline:
            return True
        if prog:
            try:
                if prog.iscanceled():
                    return True
            except Exception:
                pass
        return False

    def _fetch_folder(fid):
        if _should_abort_folder():
            return
        try:
            thumb = api.get_folder_thumbnail(fid)
            _note_thumb(fid, thumb)
            if not thumb:
                log("folder %s: DEFINITIVE no-thumb (page loaded, no og:image)"
                    % fid, xbmc.LOGINFO)
        except FilesterError as e:
            if getattr(e, "transient", False):
                failed_folders[0] += 1
                log("folder %s: TRANSIENT failure — %r" % (fid, e), xbmc.LOGWARNING)
            else:
                failed_folders[0] += 1
                log("folder %s: NON-transient error — %r" % (fid, e), xbmc.LOGERROR)
        except Exception as e:
            failed_folders[0] += 1
            log("folder %s: EXCEPTION — %r" % (fid, e), xbmc.LOGWARNING)

    def _fetch_file(fobj):
        if _should_abort_file():
            return
        try:
            resolve_file_thumbnail(api, fobj)
        except Exception:
            failed_files[0] += 1

    pool = ThreadPoolExecutor(max_workers=12)
    try:
        if folders:
            folder_futures = [pool.submit(_fetch_folder, fid) for fid in folders]
            for fut in as_completed(folder_futures):
                done[0] += 1
                _update()
                try:
                    fut.result(timeout=0.1)
                except Exception:
                    pass
                if cancelled[0]:
                    for f in folder_futures:
                        f.cancel()
                    break

        if files and not cancelled[0]:
            file_futures = [pool.submit(_fetch_file, fobj) for fobj in files]
            for fut in as_completed(file_futures):
                done[0] += 1
                _update()
                try:
                    fut.result(timeout=0.1)
                except Exception:
                    pass
                if _should_abort_file():
                    for f in file_futures:
                        f.cancel()
                    break
    finally:
        pool.shutdown(wait=False)
        try:
            if prog:
                prog.close()
        except Exception:
            pass

    try:
        flush_disk_cache(force=True)
    except Exception:
        pass

    log("prefetch: %d/%d resolved in %.1fs — folders %d/%d, files %d/%d"
        % (done[0], total, time.time() - start_ts,
           len(folders) - failed_folders[0], len(folders),
           len(files) - failed_files[0], len(files)))

    if failed_folders[0] > 0:
        try:
            xbmcgui.Dialog().notification(
                "Filester",
                "%d folder thumbnail(s) failed — reopen to retry"
                % failed_folders[0],
                xbmcgui.NOTIFICATION_INFO, 3000)
        except Exception:
            pass


def settings_api():
    return FilesterAPI(
        api_key=ADDON.getSetting("api_key") or "",
        cache_buster=ADDON.getSetting("cache_buster") == "true",
        verify_ssl=ADDON.getSetting("ssl_verify") != "false",
        ssl_fallback=ADDON.getSetting("ssl_fallback") != "false",
        user_agent=ADDON.getSetting("view_ua") or "Kodi/20.0 (Windows NT 10.0; Win64; x64)",
    )


def per_page():
    try:
        return min(100, max(20, int(float(ADDON.getSetting("per_page") or 100))))
    except (ValueError, TypeError):
        return 100


def default_sort():
    return (ADDON.getSetting("default_sort") or "newest").strip() or "newest"


def use_folder_endpoint():
    return (ADDON.getSetting("folder_id_mode") or "files-filter") == "folder-files"


def need_api_key():
    if ADDON.getSetting("api_key"):
        return False
    xbmcgui.Dialog().ok(_(30100), _(30101))
    ADDON.openSettings()
    return not bool(ADDON.getSetting("api_key"))


def url_for(**kwargs):
    return BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in kwargs.items() if v is not None})


def fmt_size(num):
    try:
        num = int(num or 0)
    except (ValueError, TypeError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return "%3.1f %s" % (num, unit) if unit != "B" else "%d B" % num
        num /= 1024.0
    return "%d B" % num


def fmt_date(iso):
    s = str(iso or "")
    return s[:10] if len(s) >= 10 else s


def handle_error(exc):
    if isinstance(exc, RateLimitError):
        xbmcgui.Dialog().ok("Filester", _(30138))
    elif isinstance(exc, FilesterError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
        xbmcgui.Dialog().ok("Filester", _(30146))
    elif isinstance(exc, FilesterError) and "missing-slug" in str(exc):
        xbmcgui.Dialog().ok("Filester", _(30141))
    else:
        log("error: %r" % exc, xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Filester", _(30145), xbmcgui.NOTIFICATION_ERROR)


# ---------- tree utils ----------

def build_tree(folders):
    by_id = {}
    for f in folders or []:
        fid = folder_id_of(f)
        if fid:
            by_id[str(fid)] = f
    roots, kids = [], {}
    for f in folders or []:
        fid = str(folder_id_of(f) or "")
        parent = f.get("parent")
        parent = str(parent) if parent else None
        if parent and parent in by_id:
            kids.setdefault(parent, []).append(f)
        else:
            roots.append(f)
    return roots, kids


def pick_folder_dialog(api, title, exclude_ids=None, _folders=None):
    excluded = set(str(x) for x in (exclude_ids or set()))
    try:
        folders = _folders if _folders is not None else api.get_folders()
    except Exception as e:
        handle_error(e)
        return None
    roots, kids = build_tree(folders)
    options, values = [_(30134)], [""]

    def _walk(nodes, depth):
        for n in sorted(nodes, key=lambda x: str(x.get("name") or "").lower()):
            nid = str(folder_id_of(n) or "")
            if nid in excluded:
                continue
            options.append("%s%s" % ("  " * depth + "• " if depth else "", n.get("name") or nid))
            values.append(nid)
            _walk(kids.get(nid, []), depth + 1)
    _walk(roots, 0)
    idx = xbmcgui.Dialog().select(title, options)
    if idx < 0:
        return None
    return values[idx]


# ---------- listings ----------

def add_folder_node(folder, kids, sort_mode):
    fid = str(folder_id_of(folder))
    name = folder.get("name") or fid
    sub = len(kids.get(fid, []))
    label = name if not sub else "%s [COLOR grey](%d)[/COLOR]" % (name, sub)
    li = xbmcgui.ListItem(label=label)
    thumb = cached_folder_thumb(folder)
    art = {"icon": "DefaultFolder.png"}
    if thumb:
        art["thumb"] = thumb
    li.setArt(art)
    li.setInfo("video", {"title": name, "plot": "Folder: %s" % name})
    ctx = [
        (_(30106), "RunPlugin(%s)" % url_for(action="create_folder", parent=fid)),
        (_(30112), "RunPlugin(%s)" % url_for(action="set_thumb", folder=fid)),
        (_(30110), "RunPlugin(%s)" % url_for(action="move_folder", folder=fid)),
        (_(30111), "RunPlugin(%s)" % url_for(action="delete_folder", folder=fid, name=name)),
        (_(30113), "Container.Refresh"),
    ]
    li.addContextMenuItems(ctx)
    xbmcplugin.addDirectoryItem(HANDLE, url_for(action="folder", folder=fid, page=1), li, True)


def add_file_node(fobj, folder=None, current_folder_name=None, folder_names=None,
                  show_folder_label=False):
    fid = str(fobj.get("id") or fobj.get("uuid") or "")
    name = fobj.get("name") or fobj.get("original_name") or fid
    size = fmt_size(fobj.get("size"))
    date = fmt_date(fobj.get("created_at"))
    ftype = (fobj.get("type") or "").lower()
    is_video = _is_video(fobj)

    # Folder name for the Filester.FolderName property (skins) and, when
    # show_folder_label is True, appended to the visible label too.
    file_folder_id = fobj.get("folder_id")
    folder_label = None
    if file_folder_id:
        if folder and str(file_folder_id) == str(folder) and current_folder_name:
            folder_label = current_folder_name
        elif folder_names:
            folder_label = folder_names.get(str(file_folder_id))

    base_label = name if is_video else "%s [COLOR grey][%s][/COLOR]" % (name, (ftype or "file"))
    if show_folder_label and folder_label:
        label = "%s [COLOR grey]— %s[/COLOR]" % (base_label, folder_label)
    else:
        label = base_label

    li = xbmcgui.ListItem(label=label)
    thumb = cached_file_thumb(fobj)
    art = {"icon": "DefaultVideo.png" if is_video else "DefaultFile.png"}
    if thumb:
        art["thumb"] = thumb
        art["poster"] = thumb
    li.setArt(art)
    plot_bits = []
    if size:
        plot_bits.append("Size: %s" % size)
    if date:
        plot_bits.append("Date: %s" % date)
    if fobj.get("views") not in (None, ""):
        plot_bits.append("Views: %s" % fobj.get("views"))
    if fobj.get("downloads") not in (None, ""):
        plot_bits.append("Downloads: %s" % fobj.get("downloads"))
    li.setInfo("video", {"title": name, "plot": " | ".join(plot_bits), "date": date})
    if folder_label:
        li.setProperty("Filester.FolderName", folder_label)
    play_url = url_for(action="play", file=fid)
    info_url = url_for(action="info", file=fid)
    if is_video:
        li.setProperty("IsPlayable", "true")
        item_url = play_url
    else:
        item_url = info_url
    ctx = [
        (_(30108), "RunPlugin(%s)" % play_url),
        (_(30109), "RunPlugin(%s)" % info_url),
        (_(30110), "RunPlugin(%s)" % url_for(action="move_file", file=fid, name=name)),
        (_(30111), "RunPlugin(%s)" % url_for(action="delete_file", file=fid, name=name)),
        (_(30113), "Container.Refresh"),
    ]
    li.addContextMenuItems(ctx)
    xbmcplugin.addDirectoryItem(HANDLE, item_url, li, False)


def list_root(params):
    api = settings_api()
    sort_mode = params.get("sort") or default_sort()

    # Folder list cached in session — /folders changes rarely and refetching
    # it on every root open (including Back from a folder) is slow.
    folders = None
    cache_key = "Filester.RootFolders"
    try:
        cached_json = xbmcgui.Window(10000).getProperty(cache_key) or ""
        if cached_json:
            folders = json.loads(cached_json)
    except Exception:
        folders = None

    if folders is None:
        try:
            folders = api.get_folders()
        except Exception as e:
            handle_error(e)
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
            return
        try:
            xbmcgui.Window(10000).setProperty(cache_key, json.dumps(folders))
        except Exception:
            pass

    roots, kids = build_tree(folders)

    from filester_api import sort_folders
    sorted_roots = sort_folders(roots, sort_mode if sort_mode in ("az", "za") else "az")

    try:
        items, _pag = api.get_files(page=1, per_page=per_page(), folder=None)
        root_only = [x for x in items if not x.get("folder_id")]
        show = root_only if root_only else items
        sorted_files = sort_files(show, sort_mode)[: per_page()]
    except Exception as e:
        log("root files failed: %r" % e)
        sorted_files = []

    # Prefetch BEFORE building items — Kodi captures listitem art at
    # addDirectoryItem time, so setting it afterwards has no effect.
    missing_folders = [str(folder_id_of(f)) for f in sorted_roots
                       if cached_folder_thumb(f) is None]
    missing_files = [f for f in sorted_files
                     if cached_file_thumb(f) is None]
    _prefetch_thumbs_sync(missing_folders, missing_files, budget=30)

    # folder_id → name map, so files shown at root (either loose root
    # files or, in the fallback case, files pulled from other folders)
    # can display their parent folder.
    folder_names = {}
    for f in folders or []:
        fname = f.get("name")
        fkey = str(folder_id_of(f) or "")
        if fkey and fname:
            folder_names[fkey] = fname

    for f in sorted_roots:
        try:
            add_folder_node(f, kids, sort_mode)
        except Exception as e:
            log("folder node failed: %r" % e)
    for fobj in sorted_files:
        add_file_node(fobj, folder_names=folder_names, show_folder_label=True)

    xbmcplugin.setContent(HANDLE, "videos")
    xbmcplugin.endOfDirectory(HANDLE)


def list_folder(params):
    api = settings_api()
    folder = params.get("folder") or ""
    page = max(1, int(params.get("page") or 1))
    sort_mode = params.get("sort") or default_sort()
    pp = per_page()
    if not folder:
        list_root(params)
        return
    try:
        folders = api.get_folders()
    except Exception as e:
        handle_error(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    _roots, kids = build_tree(folders)
    me = None
    for f in folders or []:
        if str(folder_id_of(f) or "") == str(folder):
            me = f
            break

    # Subfolders shown ONLY on page 1. Page 2+ is files-only.
    sorted_subs = []
    if page == 1:
        from filester_api import sort_folders
        subs = kids.get(str(folder), [])
        sorted_subs = sort_folders(subs, sort_mode if sort_mode in ("az", "za") else "az")

    prog = None
    try:
        prog = xbmcgui.DialogProgress()
        prog.create("Filester", _(30142))
    except Exception:
        prog = None

    def _on_progress(done, total):
        try:
            if prog and total:
                prog.update(int(done * 100 / total), "%d / %d" % (done, total))
        except Exception:
            pass

    try:
        all_items = api.get_all_files(
            folder=folder, per_page=pp,
            use_folder_endpoint=use_folder_endpoint(),
            progress=_on_progress)
        try:
            if prog:
                prog.close()
        except Exception:
            pass
        prog = None
        all_items = sort_files(all_items, sort_mode)
    except Exception as e:
        try:
            if prog:
                prog.close()
        except Exception:
            pass
        handle_error(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    total = len(all_items)
    pages = max(1, (total + pp - 1) // pp)
    page = min(page, pages)
    chunk = all_items[(page - 1) * pp: page * pp]

    missing_folders = [str(folder_id_of(f)) for f in sorted_subs
                       if cached_folder_thumb(f) is None]
    missing_files = [f for f in chunk if cached_file_thumb(f) is None]
    _prefetch_thumbs_sync(missing_folders, missing_files, budget=30)

    for f in sorted_subs:
        try:
            add_folder_node(f, kids, sort_mode)
        except Exception as e:
            log("subfolder node failed: %r" % e)
    for fobj in chunk:
        # No folder name passed — files inside a folder don't need to
        # repeat the folder they're already in.
        add_file_node(fobj, folder=folder)

    if page < pages:
        li = xbmcgui.ListItem(label="%s (%d/%d)" % (_(30107), page + 1, pages))
        li.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            HANDLE, url_for(action="folder", folder=folder, page=page + 1, sort=sort_mode), li, True)

    if not sorted_subs and not chunk:
        xbmcgui.Dialog().notification("Filester", _(30143), xbmcgui.NOTIFICATION_INFO)

    title = (me or {}).get("name") if me else ""
    try:
        if title:
            xbmcplugin.setPluginCategory(HANDLE, str(title))
    except Exception:
        pass
    xbmcplugin.setContent(HANDLE, "videos")
    xbmcplugin.endOfDirectory(HANDLE)


def list_all_files(params):
    api = settings_api()
    page = max(1, int(params.get("page") or 1))
    sort_mode = params.get("sort") or default_sort()
    pp = per_page()

    prog = None
    try:
        prog = xbmcgui.DialogProgress()
        prog.create("Filester", _(30142))
    except Exception:
        prog = None

    def _on_progress(done, total):
        try:
            if prog and total:
                prog.update(int(done * 100 / total), "%d / %d" % (done, total))
        except Exception:
            pass

    try:
        items = api.get_all_files(folder=None, per_page=pp, progress=_on_progress)
        try:
            if prog:
                prog.close()
        except Exception:
            pass
        prog = None
        items = sort_files(items, sort_mode)
        total = len(items)
        pages = max(1, (total + pp - 1) // pp)
        page = min(page, pages)
        chunk = items[(page - 1) * pp: page * pp]
    except Exception as e:
        try:
            if prog:
                prog.close()
        except Exception:
            pass
        handle_error(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    # folder_id → name map for Filester.FolderName property.
    # All Files shows files from every folder, so each item benefits from
    # knowing which folder it came from. Property only (no visible label —
    # the list is already known to be cross-folder).
    folder_names = {}
    try:
        folders = api.get_folders()
        for f in folders or []:
            fname = f.get("name")
            fkey = str(folder_id_of(f) or "")
            if fkey and fname:
                folder_names[fkey] = fname
    except Exception as e:
        log("folder name map failed: %r" % e)

    missing_files = [f for f in chunk if cached_file_thumb(f) is None]
    _prefetch_thumbs_sync([], missing_files, budget=30)

    for fobj in chunk:
        add_file_node(fobj, folder_names=folder_names)

    if page < pages:
        li = xbmcgui.ListItem(label="%s (%d/%d)" % (_(30107), page + 1, pages))
        xbmcplugin.addDirectoryItem(
            HANDLE, url_for(action="all_files", page=page + 1, sort=sort_mode), li, True)

    xbmcplugin.setContent(HANDLE, "videos")
    xbmcplugin.endOfDirectory(HANDLE)


def do_search(params):
    api = settings_api()
    q = (xbmcgui.Dialog().input(_(30135), defaultt=params.get("search") or "") or "").strip()
    if not q:
        xbmcgui.Dialog().notification("Filester", _(30105) + " — cancelled",
                                      xbmcgui.NOTIFICATION_INFO, 1500)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    q_lower = q.lower()
    sort_mode = params.get("sort") or default_sort()
    try:
        folders = api.get_folders()
        files = api.get_all_files(folder=None, per_page=100, max_pages=3)
    except Exception as e:
        handle_error(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    try:
        _roots, kids = build_tree(folders)
        matched_folders = [f for f in (folders or [])
                           if q_lower in str(f.get("name") or "").lower()]
        matches = [x for x in files
                   if q_lower in str(x.get("name") or "").lower()
                   or q_lower in str(x.get("original_name") or "").lower()]
        total = len(matches)
        sorted_matches = sort_files(matches, sort_mode)[:200]

        # folder_id → name map for Filester.FolderName property (search
        # results span folders, so skins can show which folder each hit
        # came from). Property only — no visible label.
        folder_names = {}
        for f in folders or []:
            fname = f.get("name")
            fkey = str(folder_id_of(f) or "")
            if fkey and fname:
                folder_names[fkey] = fname

        missing_folders = [str(folder_id_of(f)) for f in matched_folders
                           if cached_folder_thumb(f) is None]
        missing_files = [f for f in sorted_matches if cached_file_thumb(f) is None]
        _prefetch_thumbs_sync(missing_folders, missing_files, budget=30)

        for f in matched_folders:
            try:
                add_folder_node(f, kids, sort_mode)
            except Exception:
                pass
        for fobj in sorted_matches:
            add_file_node(fobj, folder_names=folder_names)
        if total > 200:
            xbmcgui.Dialog().notification("Filester", _(30147) % (200, total),
                                          xbmcgui.NOTIFICATION_WARNING)
            log("search truncated: %d matches, showing 200" % total)
        xbmcplugin.setContent(HANDLE, "videos")
        xbmcplugin.endOfDirectory(HANDLE)
    except Exception as e:
        log("search build failed: %r" % e, xbmc.LOGERROR)
        handle_error(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


# ---------- actions ----------

def action_play(params):
    file_id = params.get("file") or ""
    if not file_id:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    api = settings_api()
    prog = xbmcgui.DialogProgress()
    try:
        prog.create("Filester", _(30140))
    except Exception:
        prog = None
    try:
        stream_url, details = api.resolve_file(file_id)
    except Exception as e:
        try:
            if prog:
                prog.close()
        except Exception:
            pass
        handle_error(e)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    try:
        if prog:
            prog.close()
    except Exception:
        pass
    name = (details or {}).get("name") or (details or {}).get("original_name") or file_id
    li = xbmcgui.ListItem(path=stream_url)
    li.setInfo("video", {"title": name})
    thumb = file_thumb_url(details or {})
    if thumb:
        li.setArt({"thumb": thumb})
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def action_info(params):
    file_id = params.get("file") or ""
    api = settings_api()
    try:
        d = api.get_file(file_id)
    except Exception as e:
        handle_error(e)
        return
    lines = [
        "Name: %s" % (d.get("name") or d.get("original_name") or ""),
        "ID: %s" % d.get("id", ""),
        "UUID: %s" % (d.get("uuid") or ""),
        "Type: %s" % (d.get("type") or ""),
        "Size: %s (%s bytes)" % (fmt_size(d.get("size")), d.get("size", "")),
        "Created: %s" % (d.get("created_at") or ""),
        "Folder: %s" % (d.get("folder_id") or "[Root]"),
        "Views: %s  Downloads: %s" % (d.get("views", ""), d.get("downloads", "")),
        "Has thumbnail: %s" % (d.get("has_thumbnail"),),
        "Album: %s" % (d.get("album_name") or ""),
    ]
    if d.get("slug"):
        lines.append("Slug: %s (streamable)" % d.get("slug"))
    else:
        lines.append("Slug: — (not streamable until details return one)")
    xbmcgui.Dialog().textViewer("Filester — %s" % (d.get("name") or file_id), "\n".join(lines))


def action_create_folder(params):
    parent = params.get("parent") or ""
    name = (xbmcgui.Dialog().input(_(30132)) or "").strip()
    if not name:
        return
    api = settings_api()
    try:
        api.create_folder(name, parent or None)
        try:
            xbmcgui.Window(10000).clearProperty("Filester.RootFolders")
        except Exception:
            pass
        xbmcgui.Dialog().notification("Filester", _(30144), xbmcgui.NOTIFICATION_INFO)
        refresh_limited()
    except Exception as e:
        handle_error(e)


def action_delete_file(params):
    file_id = params.get("file") or ""
    name = params.get("name") or file_id
    if ADDON.getSetting("confirm_delete") != "false":
        if not xbmcgui.Dialog().yesno("Filester", "%s\n%s" % (_(30130), name)):
            return
    api = settings_api()
    try:
        api.delete_files([file_id])
        try:
            xbmcgui.Window(10000).clearProperty("Filester.FileThumb." + str(file_id))
        except Exception:
            pass
        try:
            disk_cache_delete("file_" + str(file_id))
        except Exception:
            pass
        refresh_limited()
    except Exception as e:
        handle_error(e)


def action_delete_folder(params):
    folder = params.get("folder") or ""
    name = params.get("name") or folder
    if ADDON.getSetting("confirm_delete") != "false":
        if not xbmcgui.Dialog().yesno("Filester", "%s\n%s" % (_(30131), name)):
            return
    api = settings_api()
    try:
        api.delete_folders([folder])
        try:
            xbmcgui.Window(10000).clearProperty("Filester.RootFolders")
        except Exception:
            pass
        _invalidate_folder_cache(folder)
        refresh_limited()
    except Exception as e:
        handle_error(e)


def action_move_file(params):
    file_id = params.get("file") or ""
    target = pick_folder_dialog(settings_api(), _(30133))
    if target is None:
        return
    try:
        settings_api().move_file(file_id, target or None)
        xbmcgui.Dialog().notification("Filester", _(30144), xbmcgui.NOTIFICATION_INFO)
        refresh_limited()
    except Exception as e:
        handle_error(e)


def action_move_folder(params):
    folder = params.get("folder") or ""
    api = settings_api()
    try:
        folders = api.get_folders()
    except Exception as e:
        handle_error(e)
        return
    blocked = move_exclusions(folders, str(folder))
    target = pick_folder_dialog(api, _(30133), exclude_ids=blocked, _folders=folders)
    if target is None:
        return
    if target == folder:
        return
    try:
        api.move_folder(folder, target or None)
        try:
            xbmcgui.Window(10000).clearProperty("Filester.RootFolders")
        except Exception:
            pass
        xbmcgui.Dialog().notification("Filester", _(30144), xbmcgui.NOTIFICATION_INFO)
        refresh_limited()
    except Exception as e:
        handle_error(e)


def action_set_thumb(params):
    folder = params.get("folder") or ""
    dlg = xbmcgui.Dialog()
    path = dlg.browseSingle(1, _(30136), "", ".jpg|.jpeg|.png|.gif|.webp")
    if not path:
        return
    try:
        _fh = xbmcvfs.File(path)
        try:
            size = _fh.size()
        finally:
            _fh.close()
    except Exception:
        size = 0
    if size and size > THUMB_MAX_BYTES:
        dlg.ok("Filester", _(30137))
        return
    import os
    tmp = __import__("tempfile").gettempdir().rstrip("/\\") + "/filester_thumb_%d%s" % (
        int(time.time()), os.path.splitext(path)[1] or ".jpg")
    try:
        ok = xbmcvfs.copy(path, tmp)
        if not ok:
            fh = xbmcvfs.File(path, "rb")
            try:
                blob = fh.readBytes(fh.size())
            finally:
                fh.close()
            with open(tmp, "wb") as out:
                out.write(blob if isinstance(blob, (bytes, bytearray)) else bytes(blob))
    except Exception as e:
        handle_error(e)
        return
    try:
        settings_api().upload_folder_thumbnail(folder, tmp,
                                               basename=path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        _invalidate_folder_cache(folder)
        dlg.notification("Filester", _(30144), xbmcgui.NOTIFICATION_INFO)
        refresh_limited()
    except Exception as e:
        handle_error(e)
    finally:
        try:
            if __import__("os").path.exists(tmp):
                __import__("os").remove(tmp)
        except Exception:
            pass


def action_sort(params):
    cur = params.get("sort") or default_sort()
    opts = [_(30115), _(30116), _(30117), _(30118), _(30119), _(30120)]
    vals = ["newest", "oldest", "az", "za", "largest", "smallest"]
    try:
        pre = vals.index(cur)
    except ValueError:
        pre = 0
    idx = xbmcgui.Dialog().select(_(30114), opts, preselect=pre)
    if idx < 0:
        return
    ADDON.setSetting("default_sort", vals[idx])
    refresh_limited()


# ---------- router ----------

def router():
    params = dict(urllib.parse.parse_qsl(sys.argv[2][1:] if len(sys.argv) > 2 else ""))
    action = params.get("action") or "root"
    if action == "play":
        if need_api_key():
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        action_play(params)
        return
    if action in ("info", "create_folder", "delete_file", "delete_folder",
                  "move_file", "move_folder", "set_thumb", "sort"):
        if need_api_key():
            return
        {
            "info": action_info,
            "create_folder": action_create_folder,
            "delete_file": action_delete_file,
            "delete_folder": action_delete_folder,
            "move_file": action_move_file,
            "move_folder": action_move_folder,
            "set_thumb": action_set_thumb,
            "sort": action_sort,
        }[action](params)
        return
    if need_api_key():
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    if action == "folder":
        list_folder(params)
    elif action == "all_files":
        list_all_files(params)
    elif action == "search":
        do_search(params)
    else:
        list_root(params)


if __name__ == "__main__":
    router()