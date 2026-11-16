#!/usr/bin/env python3
"""download_music.py - mirror your Spotify and YouTube Music libraries to local audio.

Auth
  Spotify        OAuth (PKCE with client id only, or full OAuth with secret).
                 Browser opens on first run; token cached.
  YouTube Music  A ytmusicapi auth file. Built automatically from a Netscape
                 cookie jar (-C/--cookies) when the jar holds SAPISID cookies,
                 otherwise the OAuth device flow runs on first use.

Config precedence: CLI flag > env var > config file > built-in default.

Deps
  pip install spotipy ytmusicapi yt-dlp
  ffmpeg on PATH (yt-dlp post-processing)

Run
  python download_music.py --spotify --youtube-music --out ~/Music
  python download_music.py --config ~/.config/download-music/config.toml
  python download_music.py --self-test
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("music")

AUDIO_EXT = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".oga", ".wav", ".webm"}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

DEFAULTS = {
    "output.dir": "~/Music",
    "output.format": "mp3",
    "output.m3u": True,
    "spotify.client_id": "",
    "spotify.client_secret": "",
    "spotify.redirect_uri": "http://127.0.0.1:8888/callback",
    "spotify.cache": "~/.cache/download-music/spotify_token",
    "youtube.cookies": "",
    "youtube.auth": "~/.config/download-music/ytmusic_oauth.json",
    "youtube.headers": "",
    "youtube.client_id": "",
    "youtube.client_secret": "",
    "download.sleep": 1.5,
    "download.retries": 5,
    "download.match_threshold": 0.55,
    "download.no_tags": False,
    "download.no_thumb": False,
}
ENV = {
    "spotify.client_id": "SPOTIPY_CLIENT_ID",
    "spotify.client_secret": "SPOTIPY_CLIENT_SECRET",
    "spotify.redirect_uri": "SPOTIPY_REDIRECT_URI",
    "youtube.cookies": "DM_COOKIES",
}
# argparse dest -> dotted config key
CLI_MAP = {
    "out": "output.dir", "format": "output.format", "m3u": "output.m3u",
    "spotify_client_id": "spotify.client_id",
    "spotify_client_secret": "spotify.client_secret",
    "spotify_redirect": "spotify.redirect_uri",
    "spotify_cache": "spotify.cache",
    "cookies": "youtube.cookies",
    "ytmusic_auth": "youtube.auth",
    "ytmusic_headers": "youtube.headers",
    "ytmusic_client_id": "youtube.client_id",
    "ytmusic_client_secret": "youtube.client_secret",
    "sleep": "download.sleep", "retries": "download.retries",
    "match_threshold": "download.match_threshold",
    "no_tags": "download.no_tags", "no_thumb": "download.no_thumb",
}

# --------------------------------------------------------------- data model

@dataclass
class Track:
    title: str
    artist: str
    album: str = ""
    source: str = ""            # spotify | ytmusic
    source_id: str = ""
    duration_s: int = 0
    video_id: str | None = None  # resolved YouTube Music id

    @property
    def key(self) -> str:
        return f"{_norm(self.artist)}:::{_norm(self.title)}"

    def __str__(self) -> str:
        return f"{self.artist} - {self.title}"

_PUNCT = re.compile(r"[^\w\s]")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*$", re.I)
_PAREN = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_YT_NOISE = re.compile(
    r"[\(\[]\s*(official\s*(music\s*)?(video|audio|lyric[s]?|visualizer)|lyrics?|"
    r"audio|hd|hq|4k|mv|m/v|remaster(ed)?(\s*\d{4})?|color\s*coded)\s*[\)\]]",
    re.I)

def _norm(s: str) -> str:
    """Collapse a title/artist to a match key: ascii, lower, no parens/feat/punct."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = _PAREN.sub(" ", s.lower())
    s = _FEAT.sub(" ", s)
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", s)).strip()

def _safe(name: str, maxlen: int = 120) -> str:
    return (_ILLEGAL.sub("_", name).strip(" .")[:maxlen].strip() or "untitled")

def _clean_yt_title(title: str) -> str:
    return _YT_NOISE.sub(" ", title or "")

def _merge(tracks: dict[str, Track], t: Track) -> None:
    """Keep first metadata, but adopt a YouTube id if one side already has it."""
    cur = tracks.get(t.key)
    if cur is None:
        tracks[t.key] = t
    elif t.video_id and not cur.video_id:
        cur.video_id = t.video_id

# --------------------------------------------------------------- matching

def score_match(track: Track, cand: dict) -> float:
    """0..1 confidence that a YouTube Music candidate is the same recording."""
    t_norm = _norm(track.title)
    c_norm = _norm(_clean_yt_title(cand.get("title", "")))
    t_sim = difflib.SequenceMatcher(None, t_norm, c_norm).ratio()

    a_norm = _norm(track.artist)
    cand_artists = [_norm(x["name"]) for x in (cand.get("artists") or []) if x.get("name")]
    if a_norm and cand_artists:
        a_sim = max(difflib.SequenceMatcher(None, a_norm, c).ratio()
                    for c in cand_artists)
    else:
        a_sim = 0.5

    score = 0.7 * t_sim + 0.3 * a_sim

    cs = cand.get("duration_seconds")
    if track.duration_s and cs:
        d = abs(track.duration_s - cs)
        if d > 25:
            score *= 0.45
        elif d > 10:
            score *= 0.8
    return score

# ---------------------------------------------------------------- netflix.. no, config

def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out

def load_config(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    import tomllib
    return _flatten(tomllib.loads(p.read_text(encoding="utf-8")))

def make_resolver(args, cfg):
    def get(key: str):
        dest = next((d for d, k in CLI_MAP.items() if k == key), None)
        if dest is not None:
            raw = getattr(args, dest, None)
            if raw is not None:
                return raw
        e = ENV.get(key)
        if e and os.environ.get(e) not in (None, ""):
            return os.environ[e]
        if cfg.get(key) not in (None, ""):
            return cfg[key]
        return DEFAULTS.get(key)
    return get

# ---------------------------------------------------------------- spotify

def _sp_track(obj) -> Track | None:
    if not obj or not obj.get("id"):
        return None  # local file / removed track: nothing to resolve on YT
    artists = ", ".join(a["name"] for a in obj.get("artists", []) if a.get("name"))
    album = (obj.get("album") or {}).get("name", "")
    return Track(obj.get("name", ""), artists, album, "spotify", obj["id"],
                 (obj.get("duration_ms") or 0) // 1000)

def collect_spotify(get, log_) -> tuple[list[Track], dict[str, list[str]]]:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

    scope = "user-library-read playlist-read-private playlist-read-collaborative"
    cid = get("spotify.client_id")
    secret = get("spotify.client_secret")
    redirect = get("spotify.redirect_uri")
    cache = str(Path(get("spotify.cache")).expanduser())
    if not cid:
        raise SystemExit("spotify: set spotify.client_id (config) or SPOTIPY_CLIENT_ID")

    if secret:
        auth = SpotifyOAuth(cid, secret, redirect, scope=scope,
                            cache_path=cache, open_browser=True)
    else:
        auth = SpotifyPKCE(cid, redirect_uri=redirect, scope=scope,
                           cache_path=cache, open_browser=True)
    sp = spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=3)

    out: list[Track] = []
    playlists: dict[str, list[str]] = {}
    offset = 0
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        for it in page["items"]:
            t = _sp_track(it.get("track"))
            if t:
                out.append(t)
        if not page.get("next"):
            break
        offset += len(page["items"])
    log_.info("spotify: %d liked tracks", len(out))

    for pl in sp.current_user_playlists(limit=50)["items"]:
        if not pl or not pl.get("id"):
            continue
        keys: list[str] = []
        off = 0
        while True:
            try:
                page = sp.playlist_items(pl["id"], limit=100, offset=off)
            except Exception as e:
                log_.warning("spotify: playlist %r failed: %s", pl.get("name"), e)
                break
            for it in page["items"]:
                t = _sp_track(it.get("item") or it.get("track"))
                if t:
                    out.append(t)
                    keys.append(t.key)
            if not page.get("next"):
                break
            off += len(page["items"])
            time.sleep(0.2)  # playlist_items is the 429-prone endpoint
        if keys:
            playlists[f"sp - {pl.get('name') or pl['id']}"] = keys
        log_.info("spotify: playlist %r -> %d tracks", pl.get("name"), len(keys))
    return out, playlists

# ------------------------------------------------------------ cookies + ytmusic

def parse_cookiefile_text(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        cookies[parts[5]] = parts[6].strip()
    return cookies

def parse_cookiefile(path: Path) -> dict[str, str]:
    return parse_cookiefile_text(path.read_text(encoding="utf-8", errors="replace"))

YTM_ORIGIN = "https://music.youtube.com"

def sapisid_hash(sapisid: str, origin: str = YTM_ORIGIN) -> str:
    """Google SAPISIDHASH authorization value."""
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"

def build_ytmusic_authfile(cookies: dict[str, str], dest: Path) -> Path:
    """Write a ytmusicapi BROWSER auth file from a cookie jar.

    ytmusicapi classifies BROWSER auth by finding 'SAPISIDHASH' in the
    Authorization header, then reads __Secure-3PAPISID out of Cookie at runtime
    and refreshes the hash per request - so the stored value only needs to be
    present and well formed.
    """
    sapisid = cookies.get("__Secure-3PAPISID") or cookies.get("SAPISID")
    if not sapisid:
        raise SystemExit("cookies: no __Secure-3PAPISID / SAPISID - cannot build ytmusic auth")
    auth = {
        "Accept": "*/*",
        "Authorization": sapisid_hash(sapisid),
        "Content-Type": "application/json",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "Origin": YTM_ORIGIN,
        "User-Agent": UA,
        "X-Goog-AuthUser": "0",
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(auth, indent=4), encoding="utf-8")
    dest.chmod(0o600)
    return dest

def ensure_ytmusic_auth(get, log_) -> Path | None:
    """Return a ytmusicapi auth file path, building one from cookies when possible."""
    auth = Path(get("youtube.auth")).expanduser()
    if auth.exists():
        return auth

    cookie_src = get("youtube.cookies")
    if cookie_src:
        jar = Path(cookie_src).expanduser()
        if jar.exists():
            cookies = parse_cookiefile(jar)
            if "__Secure-3PAPISID" in cookies:
                build_ytmusic_authfile(cookies, auth)
                log_.info("ytmusic: built BROWSER auth from %s -> %s", jar, auth)
                return auth
            log_.warning("ytmusic: %s lacks __Secure-3PAPISID; usable for yt-dlp "
                         "only, library enumeration will fall back to OAuth", jar)
    return None

def login_ytmusic(get, log_) -> None:
    import ytmusicapi
    auth = Path(get("youtube.auth")).expanduser()
    cid = get("youtube.client_id")
    secret = get("youtube.client_secret")
    if not (cid and secret):
        raise SystemExit(
            "ytmusic OAuth needs your own Google OAuth client id/secret "
            "(youtube.client_id + youtube.client_secret), or supply --cookies "
            "with a logged-in jar containing __Secure-3PAPISID")
    auth.parent.mkdir(parents=True, exist_ok=True)
    log_.info("ytmusic: granting OAuth device flow, writing %s", auth)
    ytmusicapi.setup_oauth(cid, secret, filepath=str(auth), open_browser=True)
    auth.chmod(0o600)

def yt_client(get, log_):
    """Authed client if credentials exist, otherwise anonymous (search still works)."""
    from ytmusicapi import YTMusic
    hdr = get("youtube.headers")
    auth = Path(get("youtube.auth")).expanduser()
    if hdr and Path(hdr).expanduser().exists():
        return YTMusic(str(Path(hdr).expanduser()))
    if auth.exists():
        try:
            return YTMusic(str(auth))
        except Exception as e:
            log_.warning("ytmusic: cannot load %s: %s", auth, e)
    return YTMusic()

def _yt_track(obj) -> Track | None:
    vid = obj.get("videoId")
    if not vid:
        return None
    artists = ", ".join(a["name"] for a in (obj.get("artists") or []) if a.get("name"))
    album = obj.get("album")
    if isinstance(album, dict):
        album = album.get("name", "")
    return Track(obj.get("title", ""), artists, album or "", "ytmusic", vid,
                 video_id=vid, duration_s=obj.get("duration_seconds") or 0)

def collect_ytmusic(yt, log_) -> tuple[list[Track], dict[str, list[str]]]:
    out: list[Track] = []
    playlists: dict[str, list[str]] = {}
    liked = yt.get_liked_songs(limit=None)
    for t in liked.get("tracks", []):
        tr = _yt_track(t)
        if tr:
            out.append(tr)
    log_.info("ytmusic: %d liked tracks", len(out))

    for pl in yt.get_library_playlists(limit=None):
        pid = pl.get("playlistId")
        if not pid:
            continue
        try:
            full = yt.get_playlist(pid, limit=None)
        except Exception as e:
            log_.warning("ytmusic: playlist %r failed: %s", pl.get("title"), e)
            continue
        keys: list[str] = []
        for t in full.get("tracks", []):
            tr = _yt_track(t)
            if tr:
                out.append(tr)
                keys.append(tr.key)
        if keys:
            playlists[f"yt - {pl.get('title') or pid}"] = keys
        log_.info("ytmusic: playlist %r -> %d tracks", pl.get("title"), len(keys))
    return out, playlists

# --------------------------------------------------------- resolve + fetch

def resolve(track: Track, yt, threshold: float, log_) -> tuple[str | None, float]:
    """Return a YouTube Music videoId for a track plus the match score."""
    if track.video_id:
        return track.video_id, 1.0
    query = f"{track.artist} {track.title}".strip()
    best: tuple[float, str | None] = (0.0, None)
    for filt in ("songs", "videos"):
        try:
            results = yt.search(query, filter=filt, limit=8) or []
        except Exception as e:
            log_.warning("search failed for %s: %s", track, e)
            continue
        for r in results:
            vid = r.get("videoId")
            if not vid:
                continue
            sc = score_match(track, r)
            if sc > best[0]:
                best = (sc, vid)
        if best[1]:
            break  # 'songs' pool already produced a candidate
    if best[1] and best[0] >= threshold:
        return best[1], best[0]
    if best[1]:
        log_.debug("best match %.2f below threshold %.2f for %s",
                   best[0], threshold, track)
    return None, best[0]

def _existing(out_dir: Path, stem: str) -> Path | None:
    for p in out_dir.glob(stem + ".*"):
        if p.suffix.lower() in AUDIO_EXT:
            return p
    return None

def _postprocessors(get) -> list[dict]:
    pps: list[dict] = [{"key": "FFmpegExtractAudio",
                        "preferredcodec": get("output.format")}]
    if not get("download.no_tags"):
        pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    if not get("download.no_thumb"):
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return pps

def download(track: Track, out_dir: Path, get, log_) -> tuple[str, Path | None]:
    import yt_dlp

    stem = _safe(f"{track.artist} - {track.title}")
    found = _existing(out_dir, stem)
    if found:
        return "exists", found

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / stem) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": int(get("download.retries")),
        "socket_timeout": 30,
        "writethumbnail": not get("download.no_thumb"),
        "postprocessors": _postprocessors(get),
    }
    jar = get("youtube.cookies")
    if jar and Path(jar).expanduser().exists():
        opts["cookiefile"] = str(Path(jar).expanduser())

    url = f"https://music.youtube.com/watch?v={track.video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return "ok", _existing(out_dir, stem)

# ---------------------------------------------------------------- state

class State:
    """Resume ledger: key -> status/file, plus playlist membership for M3U export."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"tracks": {}, "playlists": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.data.setdefault("tracks", {})
        self.data.setdefault("playlists", {})
        self._dirty = 0

    def done(self, key: str) -> bool:
        return self.data["tracks"].get(key, {}).get("status") in ("ok", "exists")

    def put(self, key: str, **fields) -> None:
        self.data["tracks"].setdefault(key, {}).update(fields)
        self._dirty += 1
        if self._dirty >= 10:
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = 0

# ------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Mirror your Spotify and YouTube Music libraries to local audio.")
    p.add_argument("--config", help="TOML config file")
    p.add_argument("--spotify", action="store_true", default=None,
                   help="collect Spotify library")
    p.add_argument("--youtube-music", dest="ytmusic", action="store_true", default=None,
                   help="collect YouTube Music library")
    p.add_argument("--out", help="output folder")
    p.add_argument("--format", choices=["mp3", "m4a", "opus", "flac", "vorbis", "wav", "aac"],
                   help="audio codec (default mp3)")
    p.add_argument("--m3u", action="store_true", default=None,
                   help="write one .m3u8 per playlist")
    p.add_argument("--no-m3u", dest="m3u", action="store_false",
                   help="skip playlist export")
    p.add_argument("--limit", type=int, default=0, help="stop after N unique tracks")
    p.add_argument("--sleep", type=float, help="seconds between downloads")
    p.add_argument("--retries", type=int, help="yt-dlp retries per track")
    p.add_argument("--match-threshold", type=float,
                   help="min match score 0..1 to accept a search hit (default 0.55)")
    p.add_argument("--cookies", help="Netscape cookie jar (yt-dlp + ytmusic auth)")
    p.add_argument("--dry-run", action="store_true", help="resolve only, download nothing")
    p.add_argument("--refresh", action="store_true",
                   help="ignore the resume ledger and re-check every track")
    p.add_argument("--no-tags", action="store_true", default=None)
    p.add_argument("--no-thumb", action="store_true", default=None)

    p.add_argument("--spotify-client-id")
    p.add_argument("--spotify-client-secret")
    p.add_argument("--spotify-redirect")
    p.add_argument("--spotify-cache")

    p.add_argument("--ytmusic-auth", help="ytmusicapi OAuth token file")
    p.add_argument("--ytmusic-headers", help="ytmusicapi browser headers json")
    p.add_argument("--ytmusic-client-id", help="Google OAuth client id (device flow)")
    p.add_argument("--ytmusic-client-secret", help="Google OAuth client secret (device flow)")
    p.add_argument("--ytmusic-login", action="store_true",
                   help="force the OAuth device flow now")

    p.add_argument("--log", help="append log to this file")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--self-test", action="store_true",
                   help="run offline checks and exit")
    return p

def setup_logging(args) -> None:
    level = logging.DEBUG if args.verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log:
        handlers.append(logging.FileHandler(Path(args.log).expanduser(), encoding="utf-8"))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def _write_m3u(out_dir: Path, state: State, playlists: dict[str, list[str]]) -> int:
    written = 0
    for name, keys in playlists.items():
        lines = ["#EXTM3U"]
        for k in keys:
            rec = state.data["tracks"].get(k) or {}
            f = rec.get("file")
            if rec.get("status") in ("ok", "exists") and f:
                lines.append(f)
        if len(lines) > 1:
            (out_dir / f"{_safe(name)}.m3u8").write_text("\n".join(lines) + "\n",
                                                         encoding="utf-8")
            written += 1
    return written

def run(args) -> int:
    cfg = load_config(args.config)
    get = make_resolver(args, cfg)

    do_spotify = args.spotify if args.spotify is not None else bool(cfg.get("spotify.enabled", False))
    do_yt = args.ytmusic if args.ytmusic is not None else bool(cfg.get("youtube.enabled", False))
    if not (do_spotify or do_yt):
        raise SystemExit("nothing to do: pass --spotify and/or --youtube-music "
                         "(or set [spotify]/[youtube] enabled = true in the config)")

    if args.ytmusic_login:
        login_ytmusic(get, log)

    tracks: dict[str, Track] = {}
    playlists: dict[str, list[str]] = {}
    if do_spotify:
        got, pls = collect_spotify(get, log)
        for t in got:
            _merge(tracks, t)
        playlists.update(pls)
    if do_yt:
        if not ensure_ytmusic_auth(get, log) and not get("youtube.headers"):
            login_ytmusic(get, log)
        yt = yt_client(get, log)
        got, pls = collect_ytmusic(yt, log)
        for t in got:
            _merge(tracks, t)
        playlists.update(pls)
    else:
        yt = yt_client(get, log)  # anonymous client, enough to search

    queue = list(tracks.values())
    if args.limit:
        queue = queue[: args.limit]
    log.info("%d unique tracks to process", len(queue))

    out_dir = Path(get("output.dir")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    state_file = out_dir / ".download_music.state.json"
    state = State(state_file)
    if args.refresh:
        state.data["tracks"] = {}

    threshold = float(get("download.match_threshold"))
    sleep_s = float(get("download.sleep"))
    stats = {"ok": 0, "exists": 0, "skipped": 0, "unavailable": 0, "failed": 0}
    missing: list[str] = []

    for i, tr in enumerate(queue, 1):
        rec = state.data["tracks"].get(tr.key) or {}
        if not args.refresh and state.done(tr.key):
            stats["skipped"] += 1
            log.debug("[%d/%d] cached %s", i, len(queue), tr)
            continue

        vid, score = resolve(tr, yt, threshold, log)
        if not vid:
            stats["unavailable"] += 1
            missing.append(str(tr))
            state.put(tr.key, status="unavailable", video_id=None, score=score,
                      title=tr.title, artist=tr.artist)
            log.warning("[%d/%d] no match (best %.2f): %s", i, len(queue), score, tr)
            continue
        tr.video_id = vid
        if args.dry_run:
            log.info("[%d/%d] would fetch %s -> %s (%.2f)", i, len(queue), tr, vid, score)
            continue

        try:
            status, path = download(tr, out_dir, get, log)
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "too many requests" in msg or "sign in" in msg:
                log.warning("rate limited / gated, backing off 60s")
                time.sleep(60)
            stats["failed"] += 1
            missing.append(str(tr))
            state.put(tr.key, status="failed", video_id=vid, score=score,
                      error=str(e)[:300], title=tr.title, artist=tr.artist)
            log.error("[%d/%d] failed %s: %s", i, len(queue), tr, e)
            continue

        stats[status] += 1
        state.put(tr.key, status=status, video_id=vid, score=round(score, 3),
                  file=str(path.relative_to(out_dir)) if path else None,
                  title=tr.title, artist=tr.artist)
        log.info("[%d/%d] %s %s", i, len(queue), status, tr)
        if sleep_s:
            time.sleep(sleep_s)

    state.save()

    m3u_count = 0
    if get("output.m3u") and playlists:
        m3u_count = _write_m3u(out_dir, state, playlists)

    if missing:
        (out_dir / "missing.txt").write_text("\n".join(missing) + "\n", encoding="utf-8")

    report = {
        "output": str(out_dir),
        "stats": stats,
        "total_unique": len(queue),
        "playlists_exported": m3u_count,
        "missing": missing,
    }
    (out_dir / "download_report.json").write_text(json.dumps(report, indent=2),
                                                  encoding="utf-8")

    print("\n== summary ==")
    print(f"out          {out_dir}")
    print(f"downloaded   {stats['ok']}")
    print(f"already had  {stats['exists']}")
    print(f"resumed/skip {stats['skipped']}")
    print(f"unavailable  {stats['unavailable']}")
    print(f"failed       {stats['failed']}")
    if m3u_count:
        print(f"playlists    {m3u_count} .m3u8 written")
    if missing:
        print(f"missing list {out_dir / 'missing.txt'}")
    print(f"report       {out_dir / 'download_report.json'}")
    return 0 if not stats["failed"] else 1

def self_test() -> int:
    assert _norm("Kendrick Lamar feat. SZA") == "kendrick lamar"
    assert _norm("Song (Remastered 2011)") == "song"
    assert _norm("AC/DC") == "ac dc"
    assert _norm("Beyoncé") == "beyonce"
    assert _clean_yt_title("Song (Official Music Video)") .strip() == "Song"
    assert _ILLEGAL.search(_safe('a/b:c*d?"e')) is None

    d: dict[str, Track] = {}
    sp = Track("Song", "Artist", "Album", "spotify", "sid")
    yt = Track("Song", "Artist", "", "ytmusic", "vid", video_id="vid")
    _merge(d, sp)
    _merge(d, yt)
    assert len(d) == 1 and d[sp.key].source == "spotify" and d[sp.key].video_id == "vid"

    # cookie jar parsing + header build
    jar = ("# Netscape HTTP Cookie File\n"
           ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tabc123\n"
           "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\txyz456\n"
           "garbage line\n")
    c = parse_cookiefile_text(jar)
    assert c == {"SAPISID": "abc123", "HSID": "xyz456"}, c
    assert sapisid_hash("abc123").startswith("SAPISIDHASH ")

    # auth file must satisfy ytmusicapi's BROWSER detection (SAPISIDHASH + cookie)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        af = build_ytmusic_authfile({"__Secure-3PAPISID": "sap", "HSID": "h"},
                                    Path(td) / "auth.json")
        blob = json.loads(af.read_text())
        assert "SAPISIDHASH" in blob["Authorization"]
        assert "__Secure-3PAPISID=sap" in blob["Cookie"]
        assert blob["Origin"] == "https://music.youtube.com"

    # match scoring: exact beats a mismatched remix, short-circuit on duration
    src = Track("Blinding Lights", "The Weeknd", duration_s=200)
    good = {"title": "Blinding Lights", "artists": [{"name": "The Weeknd"}],
            "duration_seconds": 201}
    bad = {"title": "Blinding Lights (Cover)", "artists": [{"name": "Random Band"}],
           "duration_seconds": 431}
    assert score_match(src, good) > 0.9, score_match(src, good)
    assert score_match(src, bad) < score_match(src, good)
    print("self-test ok")
    return 0

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args)
    if args.self_test:
        return self_test()
    return run(args)

if __name__ == "__main__":
    sys.exit(main())
