"""Source browsing: Spotify library and YouTube Music library.

Every call here is blocking (network + OAuth) and is meant to run inside a
Textual thread worker.  The TUI never opens a browser: a source that needs
authorising raises ``AuthNeeded`` and the message tells the user to run
``mdownload --login`` in a shell once.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import engine

log = logging.getLogger("mdownload.sources")

# spotipy logs every HTTP error itself.  They are all handled here (and turned
# into AuthNeeded), and an unclaimed stderr write garbles a full-screen TUI.
logging.getLogger("spotipy").setLevel(logging.CRITICAL)

# Spotify serves a playlist's tracks only to its owner or a collaborator, so a
# followed playlist is read from its public embed page instead.  That page
# publishes the first 100 tracks and ignores offset/limit — there is no API
# route past it.
EMBED_CAP = 100
_EMBED = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

SPOTIFY_SCOPE = ("user-library-read user-read-recently-played "
                 "playlist-read-private playlist-read-collaborative")


class AuthNeeded(RuntimeError):
    """A source needs a one-time login that the TUI cannot host."""


class NotOwned(AuthNeeded):
    """Spotify refused a playlist the token neither owns nor collaborates on."""


def getter(cfg):
    """The dotted-key accessor the engine's collectors expect."""

    def get(key: str):
        return {
            "spotify.client_id": cfg.spotify_client_id,
            "spotify.client_secret": cfg.spotify_client_secret,
            "spotify.redirect_uri": cfg.spotify_redirect_uri,
            "spotify.cache": cfg.spotify_cache,
            "youtube.auth": cfg.ytmusic_auth,
            "youtube.headers": "",
            "youtube.cookies": cfg.cookies_file,
        }.get(key, "")

    return get


# ------------------------------------------------------------ client cache
# One authorisation per app session.  A fresh auth manager per browse action
# would re-run the browser flow every time the token write fails.
_SP: object | None = None
_YT: object | None = None


def reset() -> None:
    """Drop cached clients after the credentials change in Settings."""
    global _SP, _YT
    _SP = _YT = None


# ------------------------------------------------------------------ spotify
def _spotify_auth(cfg, open_browser: bool):
    """Auth manager. A browser only opens when ``open_browser`` is true."""
    from spotipy.cache_handler import CacheFileHandler
    from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

    if not cfg.spotify_client_id:
        raise AuthNeeded("spotify: no client_id — set it in Settings (F2), "
                         "then run `mdownload --login` in a shell")

    # spotipy's CacheFileHandler does not create the parent directory; without
    # it the token write fails and every browse would re-run the whole flow.
    cache = Path(cfg.spotify_cache).expanduser()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AuthNeeded(f"spotify: cannot create {cache.parent} ({exc})") from exc

    kwargs = dict(scope=SPOTIFY_SCOPE, cache_handler=CacheFileHandler(str(cache)),
                  open_browser=open_browser)
    if cfg.spotify_client_secret:
        return SpotifyOAuth(cfg.spotify_client_id, cfg.spotify_client_secret,
                            cfg.spotify_redirect_uri, **kwargs)
    return SpotifyPKCE(cfg.spotify_client_id,
                       redirect_uri=cfg.spotify_redirect_uri, **kwargs)


def spotify(cfg, interactive: bool = False):
    """Cached-token client. Never starts an OAuth flow - that is ``login``."""
    global _SP
    if _SP is not None:
        return _SP
    import spotipy

    auth = _spotify_auth(cfg, open_browser=False)
    cache = Path(cfg.spotify_cache).expanduser()
    token = auth.cache_handler.get_cached_token()
    if not token or not token.get("access_token"):
        raise AuthNeeded(f"spotify: no cached token at {cache} — "
                         "run `mdownload --login` in a shell once")
    if auth.is_token_expired(token) and token.get("refresh_token"):
        try:
            auth.refresh_access_token(token["refresh_token"])
        except Exception as exc:  # noqa: BLE001
            raise AuthNeeded(
                f"spotify: token refresh failed ({exc}) — delete {cache} and "
                "run `mdownload --login`") from exc

    try:
        client = spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=3)
    except Exception as exc:  # noqa: BLE001
        raise AuthNeeded(f"spotify: authorisation failed ({exc}) — "
                         "run `mdownload --login` in a shell") from exc
    _SP = client
    return client


def spotify_login(cfg) -> str:
    """Run the OAuth flow once.

    The redirect is ``http://127.0.0.1:<port>/callback``, so spotipy serves the
    callback on a loopback socket and returns as soon as the browser lands
    there - no pasted URL, no stdin prompt, nothing for a TUI to deadlock on.
    """
    import spotipy

    auth = _spotify_auth(cfg, open_browser=True)
    port = urlparse(cfg.spotify_redirect_uri).port or 80
    print(f"spotify: opening the browser — authorise there; ctrl-c aborts "
          f"(callback {cfg.spotify_redirect_uri})", flush=True)
    try:
        auth.get_access_token(check_cache=False)
    except OSError as exc:
        # A second --login (or a stale one) holds the loopback port.
        raise AuthNeeded(f"cannot listen on 127.0.0.1:{port} ({exc}) — close the "
                         f"other `mdownload --login` / free the port and retry") from exc
    client = spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=3)
    me = client.current_user()
    return me.get("display_name") or me.get("id") or "spotify user"


def _spotify_guard(what: str, fn):
    try:
        return fn()
    except AuthNeeded:
        raise
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "http_status", None)
        text = str(exc).lower()
        if status == 403:
            raise NotOwned(
                f"spotify: {what} is not yours — Spotify serves a playlist's tracks "
                f"only to its owner or a collaborator (HTTP 403)"
            ) from exc
        if status == 401 or any(w in text for w in
                                ("scope", "insufficient", "valid user authentication")):
            raise AuthNeeded(
                f"spotify: the token was refused for {what} (HTTP {status}) — the "
                f"scope changed; run `mdownload --login` once to re-grant: {SPOTIFY_SCOPE}"
            ) from exc
        raise


def spotify_recent(cfg, limit: int = 50) -> list:
    sp = spotify(cfg)
    page = _spotify_guard("recently played", lambda: sp.current_user_recently_played(limit=limit))
    out, seen = [], set()
    for item in page.get("items", []):
        obj = (item or {}).get("track") or {}
        if obj.get("type") not in (None, "track"):
            continue
        t = engine.sp_track(obj)
        if t and t.key not in seen:
            seen.add(t.key)
            out.append(t)
    return out


def spotify_likes(cfg) -> list:
    sp = spotify(cfg)
    out, offset = [], 0
    while True:
        page = _spotify_guard("liked songs",
                              lambda o=offset: sp.current_user_saved_tracks(limit=50, offset=o))
        for item in page.get("items", []):
            t = engine.sp_track(item.get("track"))
            if t:
                out.append(t)
        if not page.get("next"):
            break
        offset += len(page.get("items", []))
    return out


def spotify_playlists(cfg) -> list[dict]:
    sp = spotify(cfg)
    out, offset = [], 0
    while True:
        page = _spotify_guard("playlists",
                              lambda o=offset: sp.current_user_playlists(limit=50, offset=o))
        for pl in page.get("items", []):
            if pl and pl.get("id"):
                out.append({"id": pl["id"], "name": pl.get("name") or pl["id"],
                            "count": (pl.get("items") or pl.get("tracks")
                                      or {}).get("total", 0),
                            "source": "spotify"})
        if not page.get("next"):
            break
        offset += len(page.get("items", []))
    return out


def _spotify_items(sp, pid: str) -> list:
    """Page one playlist's items. Let spotipy's HTTP error escape to the caller."""
    out, offset = [], 0
    while True:
        page = sp.playlist_items(pid, limit=100, offset=offset)
        for item in page.get("items", []):
            item = item or {}
            # Spotify renamed the field: older responses carry "track", the
            # current Web API carries "item" (and podcasts hide in there too).
            obj = item.get("track") or item.get("item")
            if not obj or obj.get("type") not in (None, "track"):
                continue
            t = engine.sp_track(obj)
            if t:
                out.append(t)
        if not page.get("next"):
            return out
        offset += len(page.get("items", []))


def spotify_embed_tracks(pid: str) -> list:
    """Tracks of a playlist from its public embed page. No token, 100 max.

    Spotify returns 403 for ``/playlists/{id}/items`` on a playlist the caller
    neither owns nor collaborates on — no scope or token fixes that.  The embed
    page is the one route that still lists those tracks, and it stops at 100.
    """
    req = urllib.request.Request(
        f"https://open.spotify.com/embed/playlist/{pid}",
        headers={"User-Agent": "Mozilla/5.0"})
    try:
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("spotify embed fetch failed for %s: %s", pid, exc)
        return []
    m = _EMBED.search(html)
    if not m:
        return []
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, ValueError):
        return []
    out = []
    for t in entity.get("trackList") or []:
        uri = t.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue
        out.append(engine.Track(
            title=t.get("title") or "", artist=t.get("subtitle") or "",
            album="", source="spotify", source_id=uri.rsplit(":", 1)[-1],
            duration_s=int((t.get("duration") or 0) // 1000)))
    return out


def spotify_playlist_tracks(cfg, pid: str, note=None) -> list:
    """A playlist's tracks; a followed playlist falls back to the public embed.

    ``note`` is called with a one-line explanation when the embed truncates.
    """
    sp = spotify(cfg)
    try:
        return _spotify_guard("a playlist", lambda: _spotify_items(sp, pid))
    except NotOwned as exc:
        tracks = spotify_embed_tracks(pid)
        if not tracks:
            raise AuthNeeded(
                "spotify: that playlist is not yours, so the Web API refuses its "
                "tracks (HTTP 403) and its public embed page listed nothing — copy "
                "the playlist into your own account, then retry"
            ) from exc
        if note and len(tracks) >= EMBED_CAP:
            note(f"spotify: only the first {EMBED_CAP} tracks of a playlist you do "
                 f"not own are public; copy it into your own account for the rest")
        return tracks


def spotify_album_tracks(cfg, aid: str) -> list:
    sp = spotify(cfg)
    out, offset = [], 0
    while True:
        page = _spotify_guard("an album",
                              lambda o=offset: sp.album_tracks(aid, limit=50, offset=o))
        for item in page.get("items", []):
            t = engine.sp_track(item)
            if t:
                out.append(t)
        if not page.get("next"):
            break
        offset += len(page.get("items", []))
    return out


# ------------------------------------------------------------ youtube music
def yt_client(cfg):
    """Authed client, building the auth file from the cookie jar when needed."""
    global _YT
    if _YT is not None:
        return _YT
    cookies = Path(cfg.cookies_file).expanduser()
    auth = Path(cfg.ytmusic_auth).expanduser()
    try:
        built = engine.ensure_ytmusic_auth(getter(cfg), log)
    except SystemExit as exc:      # raised by the engine when the jar lacks SAPISID
        raise AuthNeeded(f"youtube music: {exc}") from exc
    if built is None:
        hint = (f"refresh {cookies} (log in to music.youtube.com and export the jar)"
                if cookies.exists() else
                f"set cookies_file in Settings (F2) or ytmusic_auth to an "
                "ytmusicapi auth json")
        raise AuthNeeded(f"youtube music: no auth file at {auth} and no usable cookie "
                         f"jar — {hint}")
    try:
        client = engine.yt_client(getter(cfg), log)
    except Exception as exc:  # noqa: BLE001
        raise AuthNeeded(f"youtube music: cannot load {auth} ({exc}) — rebuild it with "
                         "`mdownload --login`") from exc
    _YT = client
    return client


def _yt_guard(what: str, fn):
    try:
        return fn()
    except AuthNeeded:
        raise
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "401" in text or "403" in text or "Unauthorized" in text:
            raise AuthNeeded(
                f"youtube music: session rejected for {what} — the cookies in "
                "~/.config/download-music/cookies.txt expired; log in again, re-export "
                "the jar, delete the auth json and run `mdownload --login`"
            ) from exc
        if "Unable to find 'contents'" in text or "Unable to find \"contents\"" in text:
            # ytmusicapi raises this KeyError for a missing or private playlist id.
            raise AuthNeeded(
                f"youtube music: no such {what} — the id is wrong, or it is private "
                "to another account; check the link, or re-export the cookies with "
                "`mdownload --login`"
            ) from exc
        raise


def yt_liked(cfg) -> list:
    yt = yt_client(cfg)
    data = _yt_guard("liked songs", lambda: yt.get_liked_songs(limit=None))
    out = []
    for item in (data or {}).get("tracks", []):
        t = engine.yt_track(item)
        if t:
            out.append(t)
    return out


def yt_playlists(cfg) -> list[dict]:
    yt = yt_client(cfg)
    rows = _yt_guard("library playlists", lambda: yt.get_library_playlists(limit=None))
    out = []
    for pl in rows or []:
        pid = pl.get("playlistId")
        if pid:
            out.append({"id": pid, "name": pl.get("title") or pid,
                        "count": pl.get("count") or 0, "source": "ytmusic"})
    return out


def yt_playlist_tracks(cfg, pid: str) -> list:
    yt = yt_client(cfg)
    full = _yt_guard("a playlist", lambda: yt.get_playlist(pid, limit=None))
    out = []
    for item in (full or {}).get("tracks", []):
        t = engine.yt_track(item)
        if t:
            out.append(t)
    return out


def login(cfg) -> bool:
    """Authorise every source, printing each result. True when one worked."""
    reset()
    ok = False
    try:
        print(f"spotify: authorised as {spotify_login(cfg)} "
              f"(token {Path(cfg.spotify_cache).expanduser()})")
        ok = True
    except Exception as exc:  # noqa: BLE001 - reported to the user, not raised
        print(f"spotify: {type(exc).__name__}: {exc}")
    try:
        print(f"youtube music: {len(yt_liked(cfg))} liked tracks "
              f"(auth {Path(cfg.ytmusic_auth).expanduser()})")
        ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"youtube music: {type(exc).__name__}: {exc}")
    return ok


def spotify_ref(ref: str) -> tuple[str, str] | None:
    """(kind, id) for a Spotify link or ``spotify:<id>`` reference.

    Handles ``open.spotify.com/playlist/<id>``, ``/album/<id>``, ``/track/<id>``,
    the ``/embed/…`` and ``/intl-xx/…`` variants, and a bare ``spotify:<id>``.
    Anything else (a YouTube link, a search string) returns None.
    """
    low = ref.strip()
    if low.lower().startswith("spotify:"):
        sid = low.split(":", 1)[1].split("?", 1)[0].strip().strip("/")
        return ("playlist", sid) if sid else None
    if "open.spotify.com/" not in low:
        return None
    tail = low.split("open.spotify.com/", 1)[1].split("?", 1)[0]
    parts = [p for p in tail.split("/")
             if p and p != "embed" and not p.startswith("intl-")]
    if len(parts) >= 2 and parts[0] in ("playlist", "album", "track"):
        return (parts[0], parts[1])
    return None


def tracks_for_url(cfg, ref: str, note=None) -> list | None:
    """Route a pasted reference to a source collector.

    Returns a track list, or None when yt-dlp should handle the URL.  Spotify
    has no streams yt-dlp can read: handing it a Spotify link is exactly how you
    get a bare ``HTTP Error 403`` — the link has to go through the Web API.
    """
    low = ref.strip()
    if low.lower().startswith("ytmusic:"):
        return yt_playlist_tracks(cfg, low.split(":", 1)[1].strip())
    hit = spotify_ref(low)
    if not hit:
        return None
    kind, sid = hit
    if kind == "track":
        sp = spotify(cfg)
        obj = _spotify_guard("a track", lambda: sp.track(sid))
        t = engine.sp_track(obj)
        return [t] if t else []
    if kind == "album":
        return spotify_album_tracks(cfg, sid)
    return spotify_playlist_tracks(cfg, sid, note=note)


def load(source: str, key: str, cfg) -> list:
    """Dispatch for the Browse screen: source in {spotify, ytmusic}."""
    if source == "spotify":
        return {"recent": lambda: spotify_recent(cfg),
                "likes": lambda: spotify_likes(cfg),
                "playlists": lambda: spotify_playlists(cfg)}[key]()
    if source == "ytmusic":
        return {"recent": lambda: yt_liked(cfg),
                "likes": lambda: yt_liked(cfg),
                "playlists": lambda: yt_playlists(cfg)}[key]()
    raise AuthNeeded(f"unknown source {source!r}")
