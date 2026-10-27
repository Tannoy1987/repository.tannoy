# Filester for Kodi

Unofficial video addon for [Filester](https://filester.me). Browse your cloud folders, play videos, and manage everything without leaving Kodi.

## Install

1. Download `plugin.video.filester-1.0.0.zip` from this page.
2. In Kodi: **Add-ons → Install from zip file →** select the zip.
3. Open **Filester → Settings** (or context menu on the addon → Settings) and paste your **API key**.
4. Open the addon and browse.

> Needs Kodi 19 (Matrix) or newer. Only dependency is `xbmc.python` 3.0.0.

## Features (every API endpoint)

| What | How in the addon |
|---|---|
| `GET /folders` | Folder tree, nested via `parent`, flat picker for moves |
| `GET /files?page&per_page&folder=` | File listings + root view, working pagination, Next-page items |
| `GET /folder/{id}/files` | Optional listing method in settings; dedupes by `id` against the page-param bug |
| `GET /file/{id}` | Details dialog + `slug` lookup before every playback |
| `POST /folder` | "New folder…" (accepts `id` or `identifier` in reply) |
| `POST /folder/thumbnail` | "Set folder thumbnail…" (JPG/PNG/GIF/WebP, 5MB check, multipart upload) |
| `POST /file/delete`, `POST /folder/delete` | Context-menu Delete (with confirm dialog) |
| `POST /file/move`, `POST /folder/move` | Context-menu Move… (picker includes `[Root]`) |
| `POST public/view` | Stream resolver: `{server}/v2/{file}?token=…` + `\|User-Agent=…` suffix for Kodi |
| Public folder page `/f/{id}` | Folder art via `og:image` (missing tag = no art; 5xx/timeout = transient) |

Plus, per the API manual: `_t` cache-buster + `no-cache` headers on GETs, client-side sorting (API has none; `created_at` is midnight-only so numeric `id` breaks ties), 429 dialog with back-off advice, expired-cert retry on every HTTPS endpoint, transient/5xx retries (max 2, jittered backoff), and a listing-method toggle.

## Safety details (v1.1.0)

- **Cycle-safe moves** — the server accepts a folder moved into its own descendant (orphaning the subtree). The destination picker hides the folder and all its descendants, so that target can never be offered.
- **Thumbnails** — cached for the session (definitive "no thumbnail" cached, transient failures never cached), prefetched in a background thread without blocking listings, refresh throttled to ~5/min and only when you're still viewing the folder.
- **Search** — matches `name` and `original_name`; over 200 matches you get an explicit warning (never silent truncation).
- Non-video files open details instead of erroring; all file handles are closed; no deprecated `xbmc.Keyboard`; Kodi never re-sorts (no `addSortMethod`).

## Settings

- **API Key** — `Authorization: Bearer …` on every call.
- **Items per page** — 20–100 (API max 100).
- **Default sort** — newest / oldest / A–Z / Z–A / largest / smallest. Always global: every page is fetched, the combined list is sorted, then paginated for display (per-page sorting would show wrong order).
- **Folder listing method** — `files-filter` (correct pagination) vs `folder-files` (buggy endpoint, deduped). Both feed the same global-sort pipeline.
- **CDN cache-buster** — `_t` + no-cache, like the web UI.
- **Verify SSL / Auto-retry past expired certificate** — for the Oct-2026 `u1.filester.me` expiry.
- **Show folder thumbnails** — one small public-page fetch per folder.
- **Confirm before delete**.

## Notes

- `slug` only comes from `GET /file/{id}` — playback always fetches details first, then calls `public/view`. Same details call also supplies file `thumbnail_url`s that are null in list responses (with `/t/{uuid}` as a best-effort fallback; 404s are never retried).
- `has_thumbnail: false` means no art and is never re-fetched; resolved thumbnail URLs are cached for 15 minutes (a web-UI replacement shows up on its own).
- Folder create returns `identifier`; listings return `id` — the addon accepts both.
- `GET /files?folder=` as a pagination workaround is marked "untested" in the API manual. It's the default because it avoids the known-buggy endpoint, but if folder listings ever look wrong on your account, flip **Folder listing method** to `folder-files` in settings (it dedupes by `id`).
- Rate limit is ~1000 req/hour: keep prefetch gentle, refresh ≤ ~5/min.
- Streams are session tokens — they play instantly but can't be bookmarked forever. Re-open the file for a fresh URL.
- There is no file-*upload* endpoint in the public API reference, so the addon doesn't include one. Folder-thumbnail upload IS included.
