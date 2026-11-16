"""The collection / matching engine: Spotify and YouTube Music libraries,
YouTube matching, ytmusic auth.

It is vendored from the standalone ``download_music.py`` (same file, byte for
byte) so a clone runs without anything beside it.

shortcut: vendored copy — re-copy from ``~/download_music.py`` when that script
changes (``cp ~/download_music.py src/mdownload/_engine.py``).
"""
from __future__ import annotations

from . import _engine as _e  # noqa: E402

Track = _e.Track
score_match = _e.score_match
resolve = _e.resolve
parse_cookiefile = _e.parse_cookiefile
parse_cookiefile_text = _e.parse_cookiefile_text
sapisid_hash = _e.sapisid_hash
build_ytmusic_authfile = _e.build_ytmusic_authfile
ensure_ytmusic_auth = _e.ensure_ytmusic_auth
yt_client = _e.yt_client
sp_track = _e._sp_track
yt_track = _e._yt_track
norm = _e._norm
main = _e.main
