"""Persistent settings.

Flat TOML at ``~/.config/mdownload/config.toml`` (Windows:
``%APPDATA%\\mdownload\\config.toml``). On first load the Spotify credentials and
cookie path are seeded from the older download-music config so the TUI works
without re-entering anything.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.platform == "win32":
    # Windows has no XDG dirs: roaming data is the ~/.config equivalent and
    # local data (caches, the history db) the ~/.local/state one.
    _ROAMING = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    _LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    CONFIG_DIR = _ROAMING / "mdownload"
    STATE_DIR = _LOCAL / "mdownload"
    CACHE_DIR = _LOCAL / "mdownload" / "cache"
else:
    CONFIG_DIR = Path.home() / ".config" / "mdownload"
    STATE_DIR = Path.home() / ".local" / "state" / "mdownload"
    CACHE_DIR = Path.home() / ".cache"

CONFIG_PATH = CONFIG_DIR / "config.toml"
HISTORY_PATH = STATE_DIR / "history.db"
LEGACY = Path.home() / ".config" / "download-music" / "config.toml"


@dataclass
class Config:
    format: str = "audio-only"       # video+audio | video-only | audio-only
    quality: str = "best"            # best | 2160 | 1440 | 1080 | 720 | 480 | 360
    video_codec: str = "any"         # any | avc1 | vp9 | av01 | h265
    audio_codec: str = "mp3"         # mp3 | m4a | flac | opus
    audio_bitrate: str = "192"
    subtitles: bool = False
    subtitle_langs: str = "en"
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    embed_chapters: bool = False
    sponsorblock: bool = False
    cookies_file: str = str(Path.home() / ".config" / "download-music" / "cookies.txt")
    cookies_from_browser: str = ""
    rate_limit: str = ""             # e.g. 5M
    retries: int = 5
    proxy: str = ""
    concurrent: int = 2
    dest_dir: str = str(Path.home() / "Music")
    recent_dirs: list[str] = field(default_factory=list)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_cache: str = str(CACHE_DIR / "download-music" / "spotify_token")
    ytmusic_auth: str = str(Path.home() / ".config" / "download-music" / "ytmusic_oauth.json")
    match_threshold: float = 0.55

    def remember_dir(self, path: str | None = None) -> None:
        d = str(Path(path or self.dest_dir).expanduser())
        self.recent_dirs = [d] + [x for x in self.recent_dirs if x != d]
        del self.recent_dirs[10:]


def _q(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def _seed_legacy(cfg: Config, data: dict) -> None:
    if data.get("spotify_client_id") or not LEGACY.exists():
        return
    try:
        old = tomllib.loads(LEGACY.read_text(encoding="utf-8"))
    except Exception:
        return
    sp = old.get("spotify", {}) or {}
    yt = old.get("youtube", {}) or {}
    cfg.spotify_client_id = sp.get("client_id", "") or cfg.spotify_client_id
    cfg.spotify_client_secret = sp.get("client_secret", "") or cfg.spotify_client_secret
    cfg.spotify_redirect_uri = sp.get("redirect_uri") or cfg.spotify_redirect_uri
    if sp.get("cache"):
        cfg.spotify_cache = str(Path(sp["cache"]).expanduser())
    if yt.get("auth"):
        cfg.ytmusic_auth = str(Path(yt["auth"]).expanduser())
    if yt.get("cookies"):
        cfg.cookies_file = str(Path(yt["cookies"]).expanduser())


def load(path: Path = CONFIG_PATH) -> Config:
    data: dict = {}
    if Path(path).exists():
        try:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cfg = Config()
    _seed_legacy(cfg, data)
    for f in fields(cfg):
        if f.name not in data:
            continue
        cur, v = getattr(cfg, f.name), data[f.name]
        try:
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, list):
                v = [str(x) for x in v] if isinstance(v, list) else cur
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        setattr(cfg, f.name, v)
    return cfg


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# mdownload settings - rewritten on every change"]
    for f in fields(cfg):
        v = getattr(cfg, f.name)
        if isinstance(v, bool):
            s = "true" if v else "false"
        elif isinstance(v, (int, float)):
            s = repr(v)
        elif isinstance(v, list):
            s = "[" + ", ".join(_q(str(x)) for x in v) + "]"
        else:
            s = _q(str(v))
        lines.append(f"{f.name} = {s}")
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)
